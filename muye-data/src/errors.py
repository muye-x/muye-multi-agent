"""muye-data 的稳定错误分类。

异常只携带可公开的错误码与消息。数据库异常正文、连接地址、请求文本和凭据不应
进入这些对象，避免 API 或普通日志意外泄漏下游信息。
"""

from __future__ import annotations


class DataServiceError(Exception):
    """可安全投影到 HTTP 的服务异常基类。"""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        status_code: int,
        recoverable: bool,
        trace_id: str = "",
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.status_code = status_code
        self.recoverable = recoverable
        self.trace_id = trace_id

    def with_trace_id(self, trace_id: str) -> "DataServiceError":
        """在异常尚未携带 trace ID 时补充请求关联信息。"""
        if not self.trace_id:
            self.trace_id = trace_id
        return self


class ConfigurationError(Exception):
    """本地配置无效；该错误应阻止生产运行时启动。"""


class ServiceAuthenticationError(DataServiceError):
    """调用方没有提供有效且目标绑定的服务凭据。"""

    def __init__(self) -> None:
        super().__init__(
            "AUTHENTICATION_ERROR",
            "服务认证失败",
            status_code=401,
            recoverable=False,
        )


class ServiceAuthorizationError(DataServiceError):
    """服务身份不能访问请求的逻辑 Resource 或部署修订。"""

    def __init__(self) -> None:
        super().__init__(
            "AUTHORIZATION_ERROR",
            "Resource 不可访问",
            status_code=403,
            recoverable=False,
        )


class AuthorizationUnavailableError(DataServiceError):
    """active Catalog 无法安全读取时拒绝全部 Agent 数据访问。"""

    def __init__(self) -> None:
        super().__init__(
            "AUTHORIZATION_UNAVAILABLE",
            "服务授权暂时不可用",
            status_code=503,
            recoverable=True,
        )


class InvalidRequestError(DataServiceError):
    """请求在资源相关校验阶段失败。"""

    def __init__(self, message: str, *, trace_id: str = "") -> None:
        super().__init__(
            "INVALID_REQUEST",
            message,
            status_code=400,
            recoverable=False,
            trace_id=trace_id,
        )


class ResourceNotFoundError(DataServiceError):
    """调用方引用了未注册资源。"""

    def __init__(self, *, trace_id: str = "") -> None:
        super().__init__(
            "RESOURCE_NOT_FOUND",
            "资源不存在",
            status_code=404,
            recoverable=False,
            trace_id=trace_id,
        )


class PipelineNotFoundError(DataServiceError):
    """调用方引用了资源未公开的 pipeline。"""

    def __init__(self, *, trace_id: str = "") -> None:
        super().__init__(
            "PIPELINE_NOT_FOUND",
            "Pipeline 不存在",
            status_code=404,
            recoverable=False,
            trace_id=trace_id,
        )


class DependencyError(DataServiceError):
    """数据库或 muye-llm 调用失败。"""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        recoverable: bool,
        status_code: int = 503,
        trace_id: str = "",
    ) -> None:
        super().__init__(
            error_code,
            message,
            status_code=status_code,
            recoverable=recoverable,
            trace_id=trace_id,
        )


class BackendUnavailableError(DependencyError):
    """数据库暂时不可用，可由只读调用执行有限重试。"""

    def __init__(self, *, trace_id: str = "") -> None:
        super().__init__(
            "BACKEND_UNAVAILABLE",
            "检索数据库暂时不可用",
            recoverable=True,
            trace_id=trace_id,
        )


class BackendProtocolError(DependencyError):
    """数据库返回了无法映射到公共契约的响应。"""

    def __init__(self, *, trace_id: str = "") -> None:
        super().__init__(
            "BACKEND_PROTOCOL_ERROR",
            "检索数据库响应无效",
            recoverable=False,
            status_code=502,
            trace_id=trace_id,
        )


class EmbeddingUnavailableError(DependencyError):
    """muye-llm Embedding 阶段失败。"""

    def __init__(self, *, recoverable: bool = True, trace_id: str = "") -> None:
        super().__init__(
            "EMBEDDING_UNAVAILABLE",
            "查询向量生成失败",
            recoverable=recoverable,
            status_code=503 if recoverable else 502,
            trace_id=trace_id,
        )


class RerankUnavailableError(DependencyError):
    """muye-llm Rerank 阶段失败。"""

    def __init__(self, *, recoverable: bool = True, trace_id: str = "") -> None:
        super().__init__(
            "RERANK_UNAVAILABLE",
            "重排服务暂时不可用",
            recoverable=recoverable,
            status_code=503 if recoverable else 502,
            trace_id=trace_id,
        )


class RetrievalUnavailableError(DependencyError):
    """完整召回没有任何可用通道。"""

    def __init__(self, *, trace_id: str = "") -> None:
        super().__init__(
            "RETRIEVAL_UNAVAILABLE",
            "召回服务暂时不可用",
            recoverable=True,
            trace_id=trace_id,
        )


class RetrievalTimeoutError(DependencyError):
    """完整请求超过总时间预算。"""

    def __init__(self, *, trace_id: str = "") -> None:
        super().__init__(
            "RETRIEVAL_TIMEOUT",
            "召回请求超时",
            recoverable=True,
            status_code=504,
            trace_id=trace_id,
        )


class SnapshotIdentityUnavailableError(DataServiceError):
    """当前进程没有加载版本化 Snapshot，不能作为 candidate 评测证明。"""

    def __init__(self) -> None:
        super().__init__(
            "SNAPSHOT_IDENTITY_UNAVAILABLE",
            "当前服务未加载版本化 Resource Snapshot",
            status_code=503,
            recoverable=False,
        )
