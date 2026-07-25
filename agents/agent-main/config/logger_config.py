"""
生产级统一日志配置（支持审计日志）
- 使用 root logger，保证所有 logger.info 生效
- 支持 ContextVar 注入客户端 IP
- 支持按天轮转（北京时间）
- 防止重复初始化 handler
- 自动记录时间 / IP / 文件名 / 行号
"""
import logging
import os
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler, RotatingFileHandler

# ======================
# 1. ContextVar：存客户端 IP
# ======================
client_ip_ctx: ContextVar[str] = ContextVar("client_ip", default="-")

# ======================
# 2. 日志 Filter：注入 IP
# ======================
class ClientIPFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.client_ip = client_ip_ctx.get()
        return True


# ======================
# 3. 北京时间 Formatter（核心）
# ======================
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_time_converter(timestamp: float):
    dt = datetime.fromtimestamp(timestamp, BEIJING_TZ)
    return dt.timetuple()


class BeijingFormatter(logging.Formatter):
    converter = staticmethod(beijing_time_converter)


# ======================
# 4. 全局初始化标记（防止重复配置）
# ======================
_LOGGER_INITIALIZED = False


def setup_logger(
    log_file: str = "./logs/muye_agent.log",
    level: int = logging.INFO,
    log_format: str = (
        "%(asctime)s [%(levelname)s] "
        "[IP:%(client_ip)s] "
        "%(name)s:%(lineno)d "
        "- %(message)s"
    ),
    date_format: str = "%Y-%m-%d %H:%M:%S",
    when: str = "D",
    interval: int = 1,
    backup_count: int = 7,
    use_rotating: bool = False,
    use_timed_rotating: bool = True,
    max_bytes: int = 100 * 1024 * 1024,
):
    """
    初始化 root logger（只执行一次，强制北京时间）
    """
    global _LOGGER_INITIALIZED
    if _LOGGER_INITIALIZED:
        return

    # 1. 创建日志目录
    log_dir = os.path.dirname(log_file)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    # 2. 获取 root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 防止重复 handler
    if root_logger.handlers:
        root_logger.handlers.clear()

    # 3. 配置 Formatter 与 Filter（北京时间）
    formatter = BeijingFormatter(log_format, datefmt=date_format)
    ip_filter = ClientIPFilter()

    # 4. 配置文件 handler
    if use_rotating:
        file_handler = RotatingFileHandler(
            filename=log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
    elif use_timed_rotating:
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when=when,
            interval=interval,
            backupCount=backup_count,
            encoding="utf-8",
            utc=False,  # 时间由 Formatter 控制
        )
        # 按"北京时间日期"切割
        file_handler.suffix = "%Y-%m-%d.log"
    else:
        file_handler = logging.FileHandler(
            filename=log_file,
            encoding="utf-8",
        )

    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(ip_filter)

    # 5. 配置控制台 handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(ip_filter)

    # 6. 挂载 handler
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    _LOGGER_INITIALIZED = True
    root_logger.info("日志系统初始化完成（北京时间）")


# ======================
# 5. 对外初始化入口
# ======================
def init_default_logger():
    """
    应用启动时调用一次
    """
    setup_logger(
        log_file="./logs/muye_agent.log",
        level=logging.INFO,
        backup_count=7,
    )


# ======================
# 6. 审计日志语义封装
# ======================
class AuditLogger:
    """
    审计日志语义封装（仍使用 root logger）
    """

    def __init__(self):
        self.logger = logging.getLogger("AUDIT")

    def log_action(
        self,
        session_id: str,
        action: str,
        from_state: str,
        to_state: str,
        details: dict,
    ):
        msg = (
            "[AUDIT] "
            f"Session={session_id} | "
            f"Action={action} | "
            f"State={from_state}->{to_state} | "
            f"Details={details}"
        )
        self.logger.info(msg)


# ======================
# 7. 全局审计日志实例
# ======================
_audit_logger: AuditLogger | None = None


def get_audit_logger() -> AuditLogger:
    global _audit_logger
    if _audit_logger is None:
        _audit_logger = AuditLogger()
    return _audit_logger


def log_action(
    session_id: str,
    action: str,
    from_state: str,
    to_state: str,
    details: dict,
):
    get_audit_logger().log_action(
        session_id, action, from_state, to_state, details
    )
