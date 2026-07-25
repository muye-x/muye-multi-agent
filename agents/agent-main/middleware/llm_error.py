"""LLM 调用错误分类与指数退避重试中间件。"""

import logging
import time
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse

from llm_error_classification import classify_llm_error, extract_retry_after

from .base import AgentMiddleware

logger = logging.getLogger(__name__)


class LLMErrorHandlingMiddleware(AgentMiddleware):
    """根据异常类型决定是否重试模型调用。

    配额、认证和请求错误直接失败；限流、超时、网络故障及服务端错误使用
    指数退避，并优先遵循 HTTP ``Retry-After`` 响应头。
    """

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 8.0,
    ) -> None:
        """设置最大重试次数、基础延迟和最大延迟，时间单位均为秒。"""
        super().__init__()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay

    def _classify_error(self, exc: Exception) -> str:
        """返回 ``no_retry``、``retry`` 或 ``unknown`` 分类。"""
        return classify_llm_error(exc)

    def _extract_retry_after(self, exc: Exception) -> float | None:
        """读取异常链中的 ``Retry-After`` 秒数。"""
        return extract_retry_after(exc)

    def _calculate_delay(self, attempt: int, retry_after: float | None = None) -> float:
        """计算当前尝试对应的退避秒数。"""
        if retry_after is not None:
            return min(retry_after, self.max_delay)

        # 指数退避使用 base_delay * (2 ^ attempt)。
        delay = self.base_delay * (2 ** attempt)
        return min(delay, self.max_delay)

    def _handle_model_call_with_retry(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelCallResult:
        """执行同步模型调用，并只重试可恢复错误。"""
        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return handler(request)

            except Exception as exc:
                last_exception = exc
                error_type = self._classify_error(exc)

                # 不可重试的错误，直接抛出
                if error_type == 'no_retry':
                    logger.error(
                        f"LLM调用失败（不可重试）: {type(exc).__name__}: {exc}"
                    )
                    raise

                # 最后一次尝试，不再重试
                if attempt >= self.max_retries:
                    logger.error(
                        f"LLM调用失败（已达最大重试次数 {self.max_retries}）: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise

                # 可重试的错误，计算延迟并重试
                retry_after = self._extract_retry_after(exc)
                delay = self._calculate_delay(attempt, retry_after)

                logger.warning(
                    f"LLM调用失败（第 {attempt + 1}/{self.max_retries} 次重试）: "
                    f"{type(exc).__name__}: {exc}. "
                    f"等待 {delay:.1f}s 后重试..."
                )

                time.sleep(delay)

        if last_exception:
            raise last_exception
        raise RuntimeError("重试循环未返回模型调用结果")

    async def _ahandle_model_call_with_retry(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        """执行异步模型调用，并只重试可恢复错误。"""
        import asyncio

        last_exception: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)

            except Exception as exc:
                last_exception = exc
                error_type = self._classify_error(exc)

                # 不可重试的错误，直接抛出
                if error_type == 'no_retry':
                    logger.error(
                        f"LLM调用失败（不可重试）: {type(exc).__name__}: {exc}"
                    )
                    raise

                # 最后一次尝试，不再重试
                if attempt >= self.max_retries:
                    logger.error(
                        f"LLM调用失败（已达最大重试次数 {self.max_retries}）: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    raise

                # 可重试的错误，计算延迟并重试
                retry_after = self._extract_retry_after(exc)
                delay = self._calculate_delay(attempt, retry_after)

                logger.warning(
                    f"LLM调用失败（第 {attempt + 1}/{self.max_retries} 次重试）: "
                    f"{type(exc).__name__}: {exc}. "
                    f"等待 {delay:.1f}s 后重试..."
                )

                await asyncio.sleep(delay)

        if last_exception:
            raise last_exception
        raise RuntimeError("重试循环未返回模型调用结果")

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse]
    ) -> ModelCallResult:
        """包装同步模型调用。"""
        return self._handle_model_call_with_retry(request, handler)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]]
    ) -> ModelCallResult:
        """包装异步模型调用。"""
        return await self._ahandle_model_call_with_retry(request, handler)
