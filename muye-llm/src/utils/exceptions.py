"""
LLM 服务自定义异常体系
"""


class LLMServiceException(Exception):
    """LLM 服务基础异常。"""

    def __init__(self, message: str, code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class LLMCallException(LLMServiceException):
    """LLM 调用失败（所有重试均失败）。"""

    def __init__(self, message: str = "LLM 调用失败") -> None:
        super().__init__(message, code=502)


class InvalidRequestException(LLMServiceException):
    """请求参数非法。"""

    def __init__(self, message: str = "请求参数非法") -> None:
        super().__init__(message, code=400)


class ServiceUnavailableException(LLMServiceException):
    """可选模型能力未启用或依赖当前不可用。"""

    def __init__(self, message: str = "服务能力不可用") -> None:
        super().__init__(message, code=503)
