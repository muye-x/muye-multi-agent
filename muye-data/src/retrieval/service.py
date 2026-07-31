"""只读召回编排服务。

该层负责选择 pipeline、生成查询向量、并发召回、RRF、可选重排和降级策略；
不持有任何数据写入入口，也不允许请求覆盖物理数据库目标。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from src.backends.base import (
    BackendHit,
    DenseBackendQuery,
    KeywordBackendQuery,
    RetrievalBackend,
)
from src.clients.llm import LLMModelCapabilities, MuyeLLMClient, RerankScore
from src.config import (
    DataConfig,
    DensePipelineConfig,
    HybridPipelineConfig,
    KeywordPipelineConfig,
    PipelineConfig,
    ResourceConfig,
    RerankConfig,
    ServiceSettings,
)
from src.contracts import (
    FILTER_OPERATORS,
    PipelineCapability,
    ReadinessResponse,
    ResourceReadiness,
    ResourceCapabilities,
    SnapshotIdentityResponse,
    SnapshotResourceIdentity,
    RetrievalHit,
    RetrievalResponse,
    RetrieveRequest,
    iter_filter_fields,
)
from src.errors import (
    BackendUnavailableError,
    ConfigurationError,
    DataServiceError,
    EmbeddingUnavailableError,
    InvalidRequestError,
    PipelineNotFoundError,
    ResourceNotFoundError,
    RerankUnavailableError,
    RetrievalTimeoutError,
    RetrievalUnavailableError,
    SnapshotIdentityUnavailableError,
)
from src.retrieval.fusion import rank_single_channel, weighted_rrf
from src.snapshots import LoadedResourceSnapshot, load_resource_snapshot


logger = logging.getLogger(__name__)

BackendFactory = Callable[[set[str]], Mapping[str, RetrievalBackend]]


class RetrievalService:
    """数据库无关的完整召回用例。

    ``backends`` 以配置 connection alias 索引；``llm_client`` 只负责调用
    muye-llm 的 Embedding/Rerank。服务拥有传入运行时资源，并由 ``aclose`` 统一
    关闭；测试可以注入不访问网络的 fake。
    """

    def __init__(
        self,
        *,
        config: DataConfig,
        settings: ServiceSettings,
        backends: Mapping[str, RetrievalBackend],
        llm_client: MuyeLLMClient | Any,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        self._config = config
        self._settings = settings
        self._backends = dict(backends)
        self._backend_factory = backend_factory
        self._llm_client = llm_client
        self._resources = dict(config.resources)
        self._snapshot_path: Path | None = None
        self._snapshot_mtime_ns: int | None = None
        self._snapshot_revision: str | None = None
        self._snapshot_checksum: str | None = None
        self._snapshot_identities: dict[str, SnapshotResourceIdentity] = {}
        self._validate_resource_set(self._resources)

    def _validate_resource_set(
        self,
        resources: Mapping[str, ResourceConfig],
        *,
        backends: Mapping[str, RetrievalBackend] | None = None,
    ) -> None:
        """拒绝资源 pipeline 与已构造只读适配器能力不匹配的候选资源表。"""
        selected_backends = self._backends if backends is None else backends
        required_backends = {resource.connection for resource in resources.values()}
        missing_backends = required_backends - set(selected_backends)
        if missing_backends:
            raise ConfigurationError("存在未构造的数据库 connection")
        for resource in resources.values():
            capabilities = selected_backends[resource.connection].capabilities
            if resource.fields.filterable_fields and not capabilities.filters:
                raise ConfigurationError("资源公开了 filter，但数据库适配器不支持过滤")
            for pipeline in resource.pipelines.values():
                if pipeline.type in {"dense", "hybrid"} and not capabilities.dense:
                    raise ConfigurationError("dense/hybrid pipeline 与数据库适配器能力不匹配")
                if pipeline.type in {"keyword", "hybrid"} and not capabilities.keyword:
                    raise ConfigurationError("keyword/hybrid pipeline 与数据库适配器能力不匹配")

    def _resource(self, name: str, *, trace_id: str = "") -> ResourceConfig:
        resource = self._resources.get(name)
        if resource is None:
            raise ResourceNotFoundError(trace_id=trace_id)
        return resource

    def configure_resource_snapshot(self, path: Path) -> None:
        """登记启动后轮询的 Snapshot 文件；初始候选须已由启动装配验证。"""
        if path.is_symlink():
            raise ConfigurationError("Resource Snapshot 路径不能是符号链接")
        self._snapshot_path = path
        try:
            self._snapshot_mtime_ns = path.stat().st_mtime_ns
        except OSError as exc:
            raise ConfigurationError("无法读取 Resource Snapshot 元数据") from exc

    def reload_resource_snapshot(self) -> bool:
        """完整验证候选快照后原子替换资源表；异常时保留现有服务版本。"""
        if self._snapshot_path is None:
            return False
        try:
            stat = self._snapshot_path.stat()
        except OSError as exc:
            raise ConfigurationError("无法读取 Resource Snapshot 元数据") from exc
        if self._snapshot_mtime_ns == stat.st_mtime_ns:
            return False
        candidate = load_resource_snapshot(
            self._snapshot_path,
            known_connections=set(self._config.connections),
        )
        candidate_backends = dict(self._backends)
        required_connections = {resource.connection for resource in candidate.resources.values()}
        missing_connections = required_connections - set(candidate_backends)
        if missing_connections:
            if self._backend_factory is None:
                raise ConfigurationError("候选 Resource Snapshot 需要未初始化 connection")
            staged_backends = dict(self._backend_factory(missing_connections))
            if set(staged_backends) != missing_connections:
                raise ConfigurationError("数据库 connection 工厂未返回完整且精确的候选适配器")
            candidate_backends.update(staged_backends)
        self._replace_resource_snapshot(candidate, backends=candidate_backends)
        self._backends = candidate_backends
        self._snapshot_mtime_ns = stat.st_mtime_ns
        logger.info(
            "resource snapshot reloaded revision=%s checksum=%s resource_count=%s",
            candidate.revision,
            candidate.checksum,
            len(candidate.resources),
        )
        return True

    def replace_resource_snapshot(self, snapshot: LoadedResourceSnapshot) -> None:
        """供启动装配使用已验证快照替换资源表，不暴露 HTTP 写入面。"""
        self._replace_resource_snapshot(snapshot, backends=self._backends)

    def _replace_resource_snapshot(
        self,
        snapshot: LoadedResourceSnapshot,
        *,
        backends: Mapping[str, RetrievalBackend],
    ) -> None:
        """在候选资源和适配器均验证后替换 Snapshot 派生的运行时投影。"""
        self._validate_resource_set(snapshot.resources, backends=backends)
        self._resources = dict(snapshot.resources)
        self._snapshot_revision = snapshot.revision
        self._snapshot_checksum = snapshot.checksum
        self._snapshot_identities = {
            resource_id: SnapshotResourceIdentity(
                resource_id=identity.resource_id,
                resource_revision=identity.resource_revision,
                resource_checksum=identity.resource_checksum,
                knowledge_version_id=identity.knowledge_version_id,
                collection_plan_checksum=identity.collection_plan_checksum,
            )
            for resource_id, identity in snapshot.identities.items()
        }

    def snapshot_identity(self) -> SnapshotIdentityResponse:
        """返回候选评测可核对的已加载 Snapshot 身份；静态 YAML 资源不能充当证明。"""
        if self._snapshot_revision is None or self._snapshot_checksum is None or not self._snapshot_identities:
            raise SnapshotIdentityUnavailableError()
        return SnapshotIdentityResponse(
            snapshot_revision=self._snapshot_revision,
            snapshot_checksum=self._snapshot_checksum,
            resources=dict(self._snapshot_identities),
        )

    @staticmethod
    def _pipeline(
        resource: ResourceConfig,
        name: str | None,
        *,
        trace_id: str,
    ) -> tuple[str, PipelineConfig]:
        pipeline_name = name or resource.default_pipeline
        pipeline = resource.pipelines.get(pipeline_name)
        if pipeline is None:
            raise PipelineNotFoundError(trace_id=trace_id)
        return pipeline_name, pipeline

    @staticmethod
    def _return_fields(
        resource: ResourceConfig,
        requested: list[str] | None,
        *,
        trace_id: str,
    ) -> dict[str, str]:
        selected = resource.default_return_fields if requested is None else requested
        unknown = set(selected) - set(resource.fields.exposed_fields)
        if unknown:
            raise InvalidRequestError("return_fields 包含资源未公开字段", trace_id=trace_id)
        return {name: resource.fields.exposed_fields[name] for name in selected}

    @staticmethod
    def _validate_filter(resource: ResourceConfig, request: RetrieveRequest, trace_id: str) -> None:
        unknown = iter_filter_fields(request.filter) - set(resource.fields.filterable_fields)
        if unknown:
            raise InvalidRequestError("filter 包含资源未公开字段", trace_id=trace_id)

    async def _backend_search(
        self,
        backend: RetrievalBackend,
        query: DenseBackendQuery | KeywordBackendQuery,
        *,
        trace_id: str,
    ) -> list[BackendHit]:
        """对幂等只读查询执行至多一次有限重试。"""
        last_error: BackendUnavailableError | None = None
        for attempt in range(self._settings.backend_max_retries + 1):
            try:
                async with asyncio.timeout(self._settings.backend_timeout_seconds):
                    return await backend.search(query)
            except asyncio.CancelledError:
                raise
            except BackendUnavailableError as exc:
                last_error = exc
                if attempt >= self._settings.backend_max_retries:
                    raise exc.with_trace_id(trace_id)
                await asyncio.sleep(0.05 * (2**attempt))
            except DataServiceError as exc:
                raise exc.with_trace_id(trace_id)
            except TimeoutError as exc:
                last_error = BackendUnavailableError(trace_id=trace_id)
                if attempt >= self._settings.backend_max_retries:
                    raise last_error from exc
                await asyncio.sleep(0.05 * (2**attempt))
            except Exception as exc:
                logger.warning(
                    "backend query failed trace_id=%s backend=%s error_type=%s",
                    trace_id,
                    backend.backend_type,
                    type(exc).__name__,
                )
                last_error = BackendUnavailableError(trace_id=trace_id)
                if attempt >= self._settings.backend_max_retries:
                    raise last_error from exc
                await asyncio.sleep(0.05 * (2**attempt))
        raise last_error or BackendUnavailableError(trace_id=trace_id)  # pragma: no cover

    async def _dense_recall(
        self,
        resource: ResourceConfig,
        request: RetrieveRequest,
        *,
        candidate_k: int,
        metric_type: str,
        return_fields: Mapping[str, str],
        trace_id: str,
    ) -> list[BackendHit]:
        embedding = resource.embedding
        vector_field = resource.fields.vector
        if embedding is None or vector_field is None:
            raise ConfigurationError("dense pipeline 缺少 embedding 或 vector 字段")
        try:
            async with asyncio.timeout(self._settings.llm_timeout_seconds):
                vector = await self._llm_client.embed(
                    request.query,
                    model=embedding.model,
                    expected_dimensions=embedding.dimensions,
                    trace_id=trace_id,
                )
        except asyncio.CancelledError:
            raise
        except EmbeddingUnavailableError as exc:
            raise exc.with_trace_id(trace_id)
        except TimeoutError as exc:
            raise EmbeddingUnavailableError(trace_id=trace_id) from exc
        except Exception as exc:
            logger.warning("embedding failed trace_id=%s error_type=%s", trace_id, type(exc).__name__)
            raise EmbeddingUnavailableError(trace_id=trace_id) from exc

        backend = self._backends[resource.connection]
        backend_query = DenseBackendQuery(
            target=resource.target,
            id_field=resource.fields.id,
            content_field=resource.fields.content,
            vector_field=vector_field,
            vector=tuple(vector),
            top_k=max(candidate_k, request.top_k),
            returned_fields=return_fields,
            filterable_fields=resource.fields.filterable_fields,
            filter=request.filter,
            metric_type=metric_type,
            timeout_seconds=self._settings.backend_timeout_seconds,
        )
        return await self._backend_search(backend, backend_query, trace_id=trace_id)

    async def _keyword_recall(
        self,
        resource: ResourceConfig,
        request: RetrieveRequest,
        *,
        candidate_k: int,
        return_fields: Mapping[str, str],
        trace_id: str,
    ) -> list[BackendHit]:
        keyword_field = resource.fields.keyword
        if keyword_field is None:
            raise ConfigurationError("keyword pipeline 缺少 keyword 字段")
        backend = self._backends[resource.connection]
        backend_query = KeywordBackendQuery(
            target=resource.target,
            id_field=resource.fields.id,
            content_field=resource.fields.content,
            keyword_field=keyword_field,
            text=request.query,
            top_k=max(candidate_k, request.top_k),
            returned_fields=return_fields,
            filterable_fields=resource.fields.filterable_fields,
            filter=request.filter,
            timeout_seconds=self._settings.backend_timeout_seconds,
        )
        return await self._backend_search(backend, backend_query, trace_id=trace_id)

    @staticmethod
    def _channel_failure(result: object) -> BaseException | None:
        return result if isinstance(result, BaseException) else None

    async def _hybrid_recall(
        self,
        resource: ResourceConfig,
        pipeline: HybridPipelineConfig,
        request: RetrieveRequest,
        *,
        return_fields: Mapping[str, str],
        trace_id: str,
    ) -> tuple[list[BackendHit], list[str]]:
        dense_task = asyncio.create_task(
            self._dense_recall(
                resource,
                request,
                candidate_k=pipeline.dense_candidate_k,
                metric_type=pipeline.metric_type,
                return_fields=return_fields,
                trace_id=trace_id,
            )
        )
        keyword_task = asyncio.create_task(
            self._keyword_recall(
                resource,
                request,
                candidate_k=pipeline.keyword_candidate_k,
                return_fields=return_fields,
                trace_id=trace_id,
            )
        )
        dense_result, keyword_result = await asyncio.gather(
            dense_task,
            keyword_task,
            return_exceptions=True,
        )
        dense_failure = self._channel_failure(dense_result)
        keyword_failure = self._channel_failure(keyword_result)
        for failure in (dense_failure, keyword_failure):
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            if isinstance(failure, InvalidRequestError):
                raise failure.with_trace_id(trace_id)
        if dense_failure and pipeline.dense_required:
            if isinstance(dense_failure, DataServiceError):
                raise dense_failure.with_trace_id(trace_id)
            raise RetrievalUnavailableError(trace_id=trace_id) from dense_failure
        if keyword_failure and pipeline.keyword_required:
            if isinstance(keyword_failure, DataServiceError):
                raise keyword_failure.with_trace_id(trace_id)
            raise RetrievalUnavailableError(trace_id=trace_id) from keyword_failure
        if dense_failure and keyword_failure:
            raise RetrievalUnavailableError(trace_id=trace_id)

        warnings: list[str] = []
        channels: list[tuple[Sequence[BackendHit], float]] = []
        if dense_failure:
            warnings.append("DENSE_RETRIEVAL_FAILED")
        else:
            channels.append((cast(list[BackendHit], dense_result), pipeline.dense_weight))
        if keyword_failure:
            warnings.append("KEYWORD_RETRIEVAL_FAILED")
        else:
            channels.append((cast(list[BackendHit], keyword_result), pipeline.keyword_weight))
        return weighted_rrf(channels, rank_constant=pipeline.rank_constant), warnings

    async def _rerank(
        self,
        candidates: list[BackendHit],
        query: str,
        rerank: RerankConfig,
        *,
        top_k: int,
        trace_id: str,
    ) -> list[BackendHit]:
        if not candidates:
            return []
        rerank_candidates = candidates[: self._settings.rerank_max_documents]
        try:
            async with asyncio.timeout(self._settings.llm_timeout_seconds):
                scores: list[RerankScore] = await self._llm_client.rerank(
                    query,
                    [candidate.content for candidate in rerank_candidates],
                    top_n=min(top_k, len(rerank_candidates)),
                    model=rerank.model,
                    trace_id=trace_id,
                )
        except asyncio.CancelledError:
            raise
        except RerankUnavailableError as exc:
            raise exc.with_trace_id(trace_id)
        except TimeoutError as exc:
            raise RerankUnavailableError(trace_id=trace_id) from exc
        except Exception as exc:
            logger.warning("rerank failed trace_id=%s error_type=%s", trace_id, type(exc).__name__)
            raise RerankUnavailableError(trace_id=trace_id) from exc
        ranked = [
            BackendHit(
                id=rerank_candidates[item.index].id,
                content=rerank_candidates[item.index].content,
                score=item.score,
                fields=rerank_candidates[item.index].fields,
            )
            for item in scores
        ]
        return sorted(ranked, key=lambda item: (-item.score, item.id))

    async def _retrieve(
        self,
        request: RetrieveRequest,
        *,
        trace_id: str,
    ) -> RetrievalResponse:
        started_at = time.monotonic()
        resource = self._resource(request.resource, trace_id=trace_id)
        pipeline_name, pipeline = self._pipeline(resource, request.pipeline, trace_id=trace_id)
        self._validate_filter(resource, request, trace_id)
        return_fields = self._return_fields(resource, request.return_fields, trace_id=trace_id)
        warnings: list[str] = []

        if isinstance(pipeline, DensePipelineConfig):
            candidates = rank_single_channel(
                await self._dense_recall(
                    resource,
                    request,
                    candidate_k=pipeline.candidate_k,
                    metric_type=pipeline.metric_type,
                    return_fields=return_fields,
                    trace_id=trace_id,
                )
            )
        elif isinstance(pipeline, KeywordPipelineConfig):
            candidates = rank_single_channel(
                await self._keyword_recall(
                    resource,
                    request,
                    candidate_k=pipeline.candidate_k,
                    return_fields=return_fields,
                    trace_id=trace_id,
                )
            )
        else:
            candidates, warnings = await self._hybrid_recall(
                resource,
                pipeline,
                request,
                return_fields=return_fields,
                trace_id=trace_id,
            )

        rerank = pipeline.rerank
        if rerank is not None and candidates:
            try:
                candidates = await self._rerank(
                    candidates,
                    request.query,
                    rerank,
                    top_k=request.top_k,
                    trace_id=trace_id,
                )
            except RerankUnavailableError:
                if rerank.required:
                    raise
                warnings.append("RERANK_FAILED")

        selected = candidates[: request.top_k]
        took_ms = max(0, int((time.monotonic() - started_at) * 1000))
        return RetrievalResponse(
            resource=request.resource,
            pipeline=pipeline_name,
            trace_id=trace_id,
            took_ms=took_ms,
            partial=bool(warnings),
            warnings=warnings,
            hits=[
                RetrievalHit(
                    id=hit.id,
                    content=hit.content,
                    score=float(hit.score),
                    fields=hit.fields,
                )
                for hit in selected
            ],
        )

    async def retrieve(self, request: RetrieveRequest) -> RetrievalResponse:
        """在总预算内执行完整召回，并传播调用取消。"""
        trace_id = request.resolved_trace_id()
        started_at = time.monotonic()
        try:
            async with asyncio.timeout(self._settings.total_timeout_seconds):
                result = await self._retrieve(request, trace_id=trace_id)
        except asyncio.CancelledError:
            raise
        except DataServiceError as exc:
            raise exc.with_trace_id(trace_id)
        except TimeoutError as exc:
            raise RetrievalTimeoutError(trace_id=trace_id) from exc
        logger.info(
            "retrieve completed trace_id=%s resource=%s pipeline=%s hits=%s partial=%s took_ms=%s",
            trace_id,
            result.resource,
            result.pipeline,
            len(result.hits),
            result.partial,
            int((time.monotonic() - started_at) * 1000),
        )
        return result

    def capabilities(self, resource_name: str, *, trace_id: str = "") -> ResourceCapabilities:
        """返回静态公开能力，不触发数据库连接或泄漏物理映射。"""
        resource = self._resource(resource_name, trace_id=trace_id)
        pipelines = [
            PipelineCapability(
                name=name,
                type=pipeline.type,
                rerank=pipeline.rerank is not None,
            )
            for name, pipeline in sorted(resource.pipelines.items())
        ]
        return ResourceCapabilities(
            resource=resource_name,
            default_pipeline=resource.default_pipeline,
            pipelines=pipelines,
            returnable_fields=sorted(resource.fields.exposed_fields),
            filterable_fields=sorted(resource.fields.filterable_fields),
            filter_operators=list(FILTER_OPERATORS),
            max_top_k=100,
        )

    @staticmethod
    def _pipeline_uses_llm(pipeline: PipelineConfig) -> bool:
        return pipeline.type in {"dense", "hybrid"} or pipeline.rerank is not None

    @staticmethod
    def _pipeline_readiness(
        resource: ResourceConfig,
        pipeline: PipelineConfig,
        capabilities: LLMModelCapabilities | None,
    ) -> str:
        """根据实际模型 alias 判断单个 pipeline 是否可用或可降级。"""
        embedding_models = capabilities.embedding_models if capabilities else {}
        rerank_models = capabilities.rerank_models if capabilities else frozenset()
        degraded = False

        if isinstance(pipeline, DensePipelineConfig):
            embedding = resource.embedding
            registered_dimensions = (
                embedding_models.get(embedding.model) if embedding is not None else None
            )
            if embedding is None or embedding.model not in embedding_models:
                return "unavailable"
            if registered_dimensions is not None and registered_dimensions != embedding.dimensions:
                return "unavailable"
            if registered_dimensions is None:
                degraded = True
        elif isinstance(pipeline, HybridPipelineConfig):
            embedding = resource.embedding
            registered_dimensions = (
                embedding_models.get(embedding.model) if embedding is not None else None
            )
            dense_available = (
                embedding is not None
                and embedding.model in embedding_models
                and registered_dimensions in {None, embedding.dimensions}
            )
            if not dense_available:
                if pipeline.dense_required:
                    return "unavailable"
                degraded = True
            elif registered_dimensions is None:
                degraded = True

        rerank = pipeline.rerank
        if rerank is not None and rerank.model not in rerank_models:
            if rerank.required:
                return "unavailable"
            degraded = True
        return "degraded" if degraded else "ready"

    async def readiness(self) -> tuple[ReadinessResponse, bool]:
        """汇总资源依赖状态；单个资源故障不会隐藏其他资源的可用性。"""
        llm_needed = any(
            self._pipeline_uses_llm(pipeline)
            for resource in self._resources.values()
            for pipeline in resource.pipelines.values()
        )
        llm_capabilities: LLMModelCapabilities | None = None
        if llm_needed:
            try:
                llm_capabilities = await self._llm_client.model_capabilities()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "model capability check failed error_type=%s",
                    type(exc).__name__,
                )

        async def resource_status(
            name: str,
            resource: ResourceConfig,
        ) -> tuple[str, ResourceReadiness]:
            backend = self._backends[resource.connection]
            backend_ready = await backend.health(
                resource.target,
                timeout_seconds=self._settings.backend_timeout_seconds,
            )
            pipeline_statuses = [
                self._pipeline_readiness(resource, item, llm_capabilities)
                for item in resource.pipelines.values()
            ]
            uses_llm = any(
                self._pipeline_uses_llm(item) for item in resource.pipelines.values()
            )
            if not backend_ready:
                status = "unavailable"
            elif all(item == "ready" for item in pipeline_statuses):
                status = "ready"
            elif any(item in {"ready", "degraded"} for item in pipeline_statuses):
                status = "degraded"
            else:
                status = "unavailable"

            if not uses_llm or all(item == "ready" for item in pipeline_statuses):
                llm_status = "ready"
            elif any(item in {"ready", "degraded"} for item in pipeline_statuses):
                llm_status = "degraded"
            else:
                llm_status = "unavailable"
            return name, ResourceReadiness(
                status=status,
                backend="ready" if backend_ready else "unavailable",
                llm=llm_status,
            )

        statuses = await asyncio.gather(
            *(resource_status(name, resource) for name, resource in self._resources.items())
        )
        resources = dict(statuses)
        available_count = sum(
            item.status in {"ready", "degraded"} for item in resources.values()
        )
        if available_count == len(resources):
            overall = (
                "ready"
                if all(item.status == "ready" for item in resources.values())
                else "degraded"
            )
        elif available_count:
            overall = "degraded"
        else:
            overall = "not_ready"
        return ReadinessResponse(
            status=overall,
            service="muye-data",
            resources=resources,
        ), available_count > 0

    async def aclose(self) -> None:
        """关闭 LLM 与所有唯一适配器实例，关闭失败只记录类型。"""
        close_calls = [self._llm_client.aclose()]
        seen: set[int] = set()
        for backend in self._backends.values():
            if id(backend) not in seen:
                seen.add(id(backend))
                close_calls.append(backend.aclose())
        results = await asyncio.gather(*close_calls, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("runtime close failed error_type=%s", type(result).__name__)
