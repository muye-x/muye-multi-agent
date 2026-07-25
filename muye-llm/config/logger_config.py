"""
4_llm 服务日志配置
滚动日志：每日轮换，保留 7 天，输出到 ./logs/app.log
"""
import logging
import logging.handlers
import os
from pathlib import Path


def setup_logging(log_level: str = "INFO") -> None:
    """初始化日志配置，同时输出到控制台和滚动文件，重复调用不会叠加 Handler。"""
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台 Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    # 每日滚动文件 Handler
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=log_dir / "app.log",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    if any(getattr(handler, "_muye_llm_handler", False) for handler in root.handlers):
        return
    console_handler._muye_llm_handler = True  # type: ignore[attr-defined]
    file_handler._muye_llm_handler = True  # type: ignore[attr-defined]
    root.addHandler(console_handler)
    root.addHandler(file_handler)

    # 抑制 httpx/httpcore 的冗余日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
