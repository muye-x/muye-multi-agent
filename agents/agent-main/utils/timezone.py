"""主 Agent 内部统一时区工具。"""

from datetime import datetime, timedelta, timezone

CHINA_TZ = timezone(timedelta(hours=8))


def get_china_time() -> datetime:
    """返回带时区信息的当前中国时间。"""
    return datetime.now(CHINA_TZ)
