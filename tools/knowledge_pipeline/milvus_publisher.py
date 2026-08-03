"""受控 Milvus 写侧 Publisher，仅创建不可变 KnowledgeVersion Collection。"""

from __future__ import annotations

from collections.abc import Sequence
import math
from typing import Any, Protocol
from urllib.parse import urlsplit

from contracts.models import CollectionIndexPlanV1

from .errors import DependencyUnavailableError, ParserFailedError
from .models import KnowledgeChunkV1


class MilvusPublisherProtocol(Protocol):
    """生产 Publisher 与测试 fake 共用的最小写侧接口。"""

    def publish(
        self,
        *,
        plan: CollectionIndexPlanV1,
        chunks: Sequence[KnowledgeChunkV1],
        embeddings: Sequence[Sequence[float]],
    ) -> None:
        """创建或验证 Collection，并插入与 chunks 对应的不可变数据。"""


class MilvusPublisher:
    """将确认的计划映射为固定 Milvus Schema、BM25 Function 与两个索引。

    已存在 Collection 时只验证后返回，绝不删除、truncate、upsert 或修改既有版本。
    """

    def __init__(self, *, uri: str, token: str | None = None, database: str | None = None, batch_size: int = 256) -> None:
        parsed = urlsplit(uri)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Milvus URI 必须是不含凭据的 HTTP(S) URL")
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("Milvus batch_size 必须为 1 至 10000")
        self._uri = uri.rstrip("/")
        self._token = token
        self._database = database
        self._batch_size = batch_size

    def publish(
        self,
        *,
        plan: CollectionIndexPlanV1,
        chunks: Sequence[KnowledgeChunkV1],
        embeddings: Sequence[Sequence[float]],
    ) -> None:
        """以确定性 Schema 创建 Collection；失败时保留可审计的不可变现场。"""
        if not chunks or len(chunks) != len(embeddings):
            raise ParserFailedError("Milvus 发布需要非空且一一对应的 chunks 与 embeddings")
        embedding_dimension = next(field.dimension for field in plan.fields if field.name == "embedding")
        if any(
            len(vector) != embedding_dimension or not _is_finite_vector(vector)
            for vector in embeddings
        ):
            raise ParserFailedError("Embedding 维度与 CollectionIndexPlan 不一致")
        client = self._client()
        try:
            if client.has_collection(collection_name=plan.collection_name):
                self._verify_existing_collection(client, plan)
                self._verify_existing_chunks(client, plan, chunks)
                return
            schema = client.create_schema(
                auto_id=False,
                enable_dynamic_field=False,
                description=_plan_description(plan),
            )
            for field in plan.fields:
                kwargs: dict[str, Any] = {
                    "field_name": field.name,
                    "datatype": getattr(self._data_type(), field.data_type),
                    "is_primary": field.primary_key,
                }
                if field.max_length is not None:
                    kwargs["max_length"] = field.max_length
                if field.dimension is not None:
                    kwargs["dim"] = field.dimension
                if field.enable_analyzer:
                    kwargs["enable_analyzer"] = True
                    kwargs["analyzer_params"] = {"tokenizer": "jieba"}
                schema.add_field(**kwargs)
            function, function_type = self._function_types()
            schema.add_function(
                function(
                    name=plan.bm25_function_name,
                    function_type=function_type.BM25,
                    input_field_names=["content"],
                    output_field_names=["sparse_embedding"],
                )
            )
            indexes = client.prepare_index_params()
            for index in plan.indexes:
                indexes.add_index(
                    field_name=index.field_name,
                    index_type=index.index_type,
                    metric_type=index.metric_type,
                )
            client.create_collection(collection_name=plan.collection_name, schema=schema, index_params=indexes)
            for offset in range(0, len(chunks), self._batch_size):
                batch_chunks = chunks[offset : offset + self._batch_size]
                batch_embeddings = embeddings[offset : offset + self._batch_size]
                client.insert(
                    collection_name=plan.collection_name,
                    data=[_record(chunk, vector) for chunk, vector in zip(batch_chunks, batch_embeddings, strict=True)],
                )
            client.flush(collection_name=plan.collection_name)
            client.load_collection(collection_name=plan.collection_name)
            self._verify_existing_collection(client, plan)
        except ParserFailedError:
            raise
        except Exception as exc:
            raise DependencyUnavailableError("Milvus Collection 创建或发布失败") from exc
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def _client(self) -> Any:
        """延迟导入 pymilvus，使离线契约/解析测试不要求数据库驱动。"""
        try:
            from pymilvus import MilvusClient
        except ModuleNotFoundError as exc:
            raise DependencyUnavailableError("Knowledge Worker 需要安装 pymilvus") from exc
        kwargs: dict[str, Any] = {"uri": self._uri, "timeout": 30}
        if self._token:
            kwargs["token"] = self._token
        if self._database:
            kwargs["db_name"] = self._database
        return MilvusClient(**kwargs)

    @staticmethod
    def _data_type() -> Any:
        """独立加载枚举，便于 fake Publisher 测试与驱动缺失错误区分。"""
        from pymilvus import DataType

        return DataType

    @staticmethod
    def _function_types() -> tuple[Any, Any]:
        """加载 BM25 Function 类型，不允许调用方提供原生 Function/DDL。"""
        from pymilvus import Function, FunctionType

        return Function, FunctionType

    @staticmethod
    def _verify_existing_collection(client: Any, plan: CollectionIndexPlanV1) -> None:
        """校验 schema、BM25 Function、索引和嵌入的 plan checksum，拒绝同名异构目标。"""
        try:
            description = client.describe_collection(collection_name=plan.collection_name)
        except Exception as exc:
            raise DependencyUnavailableError("无法读取已存在 Milvus Collection schema") from exc
        fields = description.get("fields") if isinstance(description, dict) else None
        if fields is None and isinstance(description, dict):
            schema = description.get("schema")
            fields = schema.get("fields") if isinstance(schema, dict) else None
        if not isinstance(description, dict) or not isinstance(fields, list):
            raise ParserFailedError("Milvus Collection schema 响应无效")
        if description.get("description") != _plan_description(plan):
            raise ParserFailedError("已存在 Milvus Collection 的 plan checksum 不匹配")
        actual_fields = {
            item.get("name"): item
            for item in fields
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        if set(actual_fields) != {field.name for field in plan.fields}:
            raise ParserFailedError("已存在 Milvus Collection 的 schema 与 CollectionIndexPlan 不匹配")
        expected_types = MilvusPublisher._data_type()
        for expected in plan.fields:
            actual = actual_fields[expected.name]
            if not _data_type_matches(actual.get("type"), getattr(expected_types, expected.data_type)):
                raise ParserFailedError(f"Milvus 字段类型不匹配：{expected.name}")
            if bool(actual.get("is_primary", False)) != expected.primary_key:
                raise ParserFailedError(f"Milvus 主键定义不匹配：{expected.name}")
            params = actual.get("params") if isinstance(actual.get("params"), dict) else {}
            if expected.max_length is not None and str(params.get("max_length")) != str(expected.max_length):
                raise ParserFailedError(f"Milvus VARCHAR 长度不匹配：{expected.name}")
            if expected.dimension is not None and str(params.get("dim")) != str(expected.dimension):
                raise ParserFailedError(f"Milvus 向量维度不匹配：{expected.name}")
            if expected.enable_analyzer and str(params.get("enable_analyzer")).lower() != "true":
                raise ParserFailedError(f"Milvus analyzer 配置不匹配：{expected.name}")
        _verify_bm25_function(description, plan)
        _verify_indexes(client, plan)

    @staticmethod
    def _verify_existing_chunks(
        client: Any,
        plan: CollectionIndexPlanV1,
        chunks: Sequence[KnowledgeChunkV1],
    ) -> None:
        """重跑只接受完整相同的不可变数据，拒绝部分或额外写入的同名 Collection。"""
        expected = {chunk.chunk_id: chunk.content_hash for chunk in chunks}
        try:
            records = client.query(
                collection_name=plan.collection_name,
                filter="",
                output_fields=["chunk_id", "content_hash"],
                limit=len(expected) + 1,
            )
        except Exception as exc:
            raise DependencyUnavailableError("无法读取已存在 Milvus Collection 的完整 chunk 集") from exc
        if not isinstance(records, list):
            raise ParserFailedError("已存在 Milvus Collection 的 chunk 响应无效")
        actual: dict[str, str] = {}
        for record in records:
            if not isinstance(record, dict):
                raise ParserFailedError("已存在 Milvus Collection 的 chunk 响应无效")
            chunk_id = record.get("chunk_id")
            content_hash = record.get("content_hash")
            if not isinstance(chunk_id, str) or not isinstance(content_hash, str):
                raise ParserFailedError("已存在 Milvus Collection 的 chunk 缺少身份或 checksum")
            if chunk_id in actual:
                raise ParserFailedError("已存在 Milvus Collection 的 chunk 主键重复")
            actual[chunk_id] = content_hash
        if actual != expected:
            raise ParserFailedError("已存在 Milvus Collection 的 chunk 与当前 KnowledgeVersion 不匹配")


def _record(chunk: KnowledgeChunkV1, embedding: Sequence[float]) -> dict[str, object]:
    """把受控 chunk 转为固定物理字段，metadata 仅用于展示。"""
    return {
        "chunk_id": chunk.chunk_id,
        "knowledge_version_id": chunk.knowledge_version_id,
        "document_id": chunk.document_id,
        "source_file_id": chunk.source_file_id,
        "content": chunk.content,
        "embedding": list(embedding),
        "title": chunk.title,
        "citation_id": chunk.citation_id,
        "source_locators": [locator.model_dump(mode="json") for locator in chunk.source_locators],
        "block_ids": chunk.block_ids,
        "chunk_index": chunk.chunk_index,
        "content_hash": chunk.content_hash,
        "metadata": {"title": chunk.title},
    }


def _plan_description(plan: CollectionIndexPlanV1) -> str:
    """将 plan checksum 写入不可变 Collection 描述，供重跑时 fail-closed 验证。"""
    return f"muye-collection-plan:{plan.plan_checksum}"


def _data_type_matches(actual: object, expected: object) -> bool:
    """兼容 pymilvus 返回 Enum 或整数的两种描述形式。"""
    return actual == expected or getattr(actual, "value", actual) == getattr(expected, "value", expected)


def _is_finite_vector(vector: Sequence[float]) -> bool:
    """拒绝 fake 或外部 Embedding 适配器传入的 NaN、Infinity 和非数值。"""
    try:
        return all(math.isfinite(float(value)) for value in vector)
    except (TypeError, ValueError):
        return False


def _verify_bm25_function(description: dict[str, Any], plan: CollectionIndexPlanV1) -> None:
    """确认 `content -> sparse_embedding` 的唯一 BM25 Function 未被替换。"""
    functions = description.get("functions")
    if not isinstance(functions, list):
        raise ParserFailedError("Milvus Collection 缺少 BM25 Function 描述")
    try:
        from pymilvus import FunctionType
    except ModuleNotFoundError as exc:  # pragma: no cover - 已在发布路径导入。
        raise DependencyUnavailableError("Knowledge Worker 需要安装 pymilvus") from exc
    for function in functions:
        if not isinstance(function, dict) or function.get("name") != plan.bm25_function_name:
            continue
        function_type = function.get("type")
        if not _data_type_matches(function_type, FunctionType.BM25):
            continue
        if function.get("input_field_names") == ["content"] and function.get("output_field_names") == [
            "sparse_embedding"
        ]:
            return
    raise ParserFailedError("Milvus Collection 的 BM25 Function 与 CollectionIndexPlan 不匹配")


def _verify_indexes(client: Any, plan: CollectionIndexPlanV1) -> None:
    """逐字段核对 Dense 与 Sparse 索引类型及 metric，缺失详情时 fail closed。"""
    for expected in plan.indexes:
        try:
            index_names = client.list_indexes(
                collection_name=plan.collection_name,
                field_name=expected.field_name,
            )
        except Exception as exc:
            raise DependencyUnavailableError("无法列出 Milvus Collection 索引") from exc
        if not isinstance(index_names, list) or not index_names:
            raise ParserFailedError(f"Milvus 缺少索引：{expected.field_name}")
        matched = False
        for index_name in index_names:
            try:
                actual = client.describe_index(
                    collection_name=plan.collection_name,
                    index_name=index_name,
                )
            except Exception as exc:
                raise DependencyUnavailableError("无法读取 Milvus 索引描述") from exc
            if not isinstance(actual, dict):
                continue
            if (
                actual.get("field_name") == expected.field_name
                and actual.get("index_type") == expected.index_type
                and actual.get("metric_type") == expected.metric_type
            ):
                matched = True
                break
        if not matched:
            raise ParserFailedError(f"Milvus 索引与 CollectionIndexPlan 不匹配：{expected.field_name}")
