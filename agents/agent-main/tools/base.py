import httpx
import asyncio
import time
from typing import Dict, Any, Optional
from abc import ABC, abstractmethod
import logging

logger = logging.getLogger(__name__)


class HTTPClientPool:
    """全局HTTP客户端连接池（单例模式）"""
    _instance: Optional[httpx.AsyncClient] = None
    _lock = asyncio.Lock()

    @classmethod
    async def get_client(cls) -> httpx.AsyncClient:
        """获取全局异步HTTP客户端（线程安全）"""
        # 使用锁保护整个检查和创建过程
        async with cls._lock:
            if cls._instance is None:
                from config import get_config
                config = get_config()

                cls._instance = httpx.AsyncClient(
                    limits=httpx.Limits(
                        max_connections=config.http_pool.max_connections,
                        max_keepalive_connections=config.http_pool.max_keepalive,
                        keepalive_expiry=config.http_pool.keepalive_expiry
                    ),
                    timeout=httpx.Timeout(float(config.api.timeout)),
                    headers={
                        'Content-Type': 'application/json',
                        'User-Agent': 'LangGraph-Intent-Service/2.0-Async'
                    }
                )
                logger.info("全局 HTTP 连接池初始化完成")
        return cls._instance

    @classmethod
    async def close(cls):
        """关闭全局客户端"""
        async with cls._lock:
            if cls._instance is not None:
                await cls._instance.aclose()
                cls._instance = None
                logger.info("全局 HTTP 连接池已关闭")

    @classmethod
    async def __aenter__(cls):
        """上下文管理器入口"""
        return await cls.get_client()

    @classmethod
    async def __aexit__(cls, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        await cls.close()


class BaseTool(ABC):
    """基础工具类，提供通用的功能和错误处理（异步版本）"""

    def __init__(self, base_url: str, timeout: int = None, retries: int = None):
        from config import get_config
        config = get_config()

        self.base_url = base_url
        self.timeout = timeout if timeout is not None else config.api.timeout
        self.retries = retries if retries is not None else config.api.retries
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """获取HTTP客户端（懒加载）"""
        if self._client is None:
            self._client = await HTTPClientPool.get_client()
        return self._client

    async def _make_request(self,
                            endpoint: str,
                            payload: Dict[str, Any],
                            method: str = 'POST') -> Dict[str, Any]:
        """统一的异步请求处理方法，包含重试和错误处理"""
        url = f"{self.base_url}/{endpoint}"
        client = await self._get_client()
        attempt = 0

        while attempt < self.retries:
            try:
                start_time = time.time()
                logger.debug(f"调用工具: {self.__class__.__name__}, URL: {url}")

                if method.upper() == 'POST':
                    response = await client.post(url, json=payload, timeout=self.timeout)
                elif method.upper() == 'GET':
                    response = await client.get(url, params=payload, timeout=self.timeout)
                else:
                    raise ValueError(f"不支持的HTTP方法: {method}")

                response.raise_for_status()
                result = response.json()

                elapsed_time = time.time() - start_time
                logger.info(f"工具调用成功: {self.__class__.__name__}, 耗时: {elapsed_time:.2f}s")

                return result

            except httpx.HTTPStatusError as e:
                attempt += 1
                logger.warning(f"工具调用失败 (尝试 {attempt}/{self.retries}): HTTP {e.response.status_code}")

                if attempt >= self.retries:
                    logger.error(f"工具调用最终失败: {self.__class__.__name__}, URL: {url}")
                    raise ToolException(f"API调用失败: HTTP {e.response.status_code}") from e

                await asyncio.sleep(2 ** attempt)

            except (httpx.RequestError, httpx.TimeoutException) as e:
                attempt += 1
                logger.warning(f"工具调用失败 (尝试 {attempt}/{self.retries}): {str(e)}")

                if attempt >= self.retries:
                    logger.error(f"工具调用最终失败: {self.__class__.__name__}, URL: {url}")
                    raise ToolException(f"API调用失败: {str(e)}") from e

                await asyncio.sleep(2 ** attempt)

        raise ToolException("达到最大重试次数，工具调用失败")

    @abstractmethod
    async def execute(self, **kwargs) -> Dict[str, Any]:
        """工具执行的具体逻辑，由子类实现（异步方法）"""
        pass


class ToolException(Exception):
    """工具调用异常"""
    pass


class ToolResult:
    """工具调用结果封装"""

    def __init__(self,
                 success: bool,
                 data: Optional[Any] = None,
                 error: Optional[str] = None,
                 metadata: Optional[Dict[str, Any]] = None):
        self.success = success
        self.data = data
        self.error = error
        self.metadata = metadata or {}

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata
        }

    @classmethod
    def success(cls, data: Any, metadata: Optional[Dict[str, Any]] = None):
        """创建成功的结果"""
        return cls(success=True, data=data, metadata=metadata)

    @classmethod
    def error(cls, error: str, metadata: Optional[Dict[str, Any]] = None):
        """创建失败的结果"""
        return cls(success=False, error=error, metadata=metadata)
