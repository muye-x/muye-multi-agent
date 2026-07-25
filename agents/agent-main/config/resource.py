"""资源清理工具 - 提供应用启动和关闭时的资源管理"""

import asyncio
import atexit
import logging
from typing import Optional

from tools.base import HTTPClientPool

logger = logging.getLogger(__name__)


class ResourceManager:
    """全局资源管理器"""

    _cleanup_registered = False

    @classmethod
    def register_cleanup_handlers(cls):
        """注册清理处理器（应在应用启动时调用一次）"""
        if cls._cleanup_registered:
            return

        # 注册 atexit 清理
        atexit.register(cls._sync_cleanup)

        cls._cleanup_registered = True
        logger.info("资源清理处理器已注册")

    @classmethod
    def _sync_cleanup(cls):
        """同步清理方法（用于 atexit）"""
        try:
            # 尝试获取或创建事件循环
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            # 运行异步清理
            if not loop.is_closed():
                loop.run_until_complete(cls.cleanup())
        except Exception as e:
            logger.error(f"资源清理失败: {str(e)}", exc_info=True)

    @classmethod
    async def cleanup(cls):
        """异步清理所有资源"""
        logger.info("开始清理全局资源...")

        # 关闭 HTTP 连接池
        await HTTPClientPool.close()

        logger.info("全局资源清理完成")

    @classmethod
    async def __aenter__(cls):
        """上下文管理器入口"""
        cls.register_cleanup_handlers()
        return cls

    @classmethod
    async def __aexit__(cls, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        await cls.cleanup()


# 自动注册清理处理器
ResourceManager.register_cleanup_handlers()
