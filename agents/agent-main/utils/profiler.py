"""
请求级性能追踪工具
用于统计每个请求各步骤的耗时，定位性能瓶颈
"""

import time
import logging
from contextvars import ContextVar
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Optional, Dict, List
from collections import OrderedDict

logger = logging.getLogger(__name__)

# 请求级 ContextVar（异步安全）
_current_profiler: ContextVar[Optional['RequestProfiler']] = ContextVar('current_profiler', default=None)


@dataclass
class TimingRecord:
    """单条耗时记录"""
    name: str
    duration_ms: float
    metadata: dict = field(default_factory=dict)
    children: List['TimingRecord'] = field(default_factory=list)


class RequestProfiler:
    """请求级性能追踪器

    使用 ContextVar 实现请求级隔离（异步安全），支持嵌套计时。
    通过环境变量 ENABLE_PROFILING 控制开关。
    """

    def __init__(self, request_id: str = "", session_id: str = ""):
        self.request_id = request_id
        self.session_id = session_id
        self.records: OrderedDict[str, TimingRecord] = OrderedDict()
        self._stack: list = []  # 嵌套追踪栈 [(name, start_time)]
        self._start_time = time.perf_counter()

    @classmethod
    def get_current(cls) -> Optional['RequestProfiler']:
        """获取当前请求的 Profiler 实例"""
        return _current_profiler.get(None)

    def activate(self):
        """激活当前 Profiler（绑定到 ContextVar）"""
        _current_profiler.set(self)

    def deactivate(self):
        """停用当前 Profiler"""
        _current_profiler.set(None)

    def start(self, name: str):
        """开始计时"""
        self._stack.append((name, time.perf_counter()))

    def stop(self, name: str) -> float:
        """停止计时并记录"""
        start_time = None

        # 从栈顶开始查找匹配的 name
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == name:
                start_time = self._stack[i][1]
                self._stack.pop(i)
                break

        if start_time is None:
            logger.warning(f"[Profiler] 未找到开始计时: {name}")
            return 0.0

        duration_ms = (time.perf_counter() - start_time) * 1000
        record = TimingRecord(name=name, duration_ms=duration_ms)

        # 添加到父级或顶层
        if self._stack:
            parent_name = self._stack[-1][0]
            if parent_name in self.records:
                self.records[parent_name].children.append(record)
            else:
                # 父级不在顶层记录中，直接添加到顶层
                self.records[name] = record
        else:
            self.records[name] = record

        return duration_ms

    @contextmanager
    def timing(self, name: str):
        """同步上下文管理器"""
        self.start(name)
        try:
            yield
        finally:
            self.stop(name)

    @asynccontextmanager
    async def async_timing(self, name: str):
        """异步上下文管理器"""
        self.start(name)
        try:
            yield
        finally:
            self.stop(name)

    def get_summary(self) -> dict:
        """返回结构化摘要"""
        total_ms = (time.perf_counter() - self._start_time) * 1000
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "total_ms": round(total_ms, 1),
            "steps": {name: round(r.duration_ms, 1) for name, r in self.records.items()},
            "slow_steps": [
                name for name, r in self.records.items()
                if r.duration_ms > 1000
            ]
        }

    def format_report(self, slow_threshold_ms: int = 1000) -> str:
        """格式化为人类可读的性能报告"""
        total_ms = (time.perf_counter() - self._start_time) * 1000
        lines = [
            "=" * 70,
            f"⏱️ 请求性能报告 [session={self.session_id}]",
            "=" * 70,
            f"总耗时: {total_ms:.0f}ms",
            "-" * 70,
            f"{'阶段':<35} {'耗时(ms)':>10} {'占比':>8}",
            "-" * 70,
        ]

        def _format_record(record: TimingRecord, indent: int = 0):
            prefix = "  " + "│ " * (indent // 2) + ("├─ " if indent > 0 else "")
            pct = (record.duration_ms / total_ms * 100) if total_ms > 0 else 0
            warn = " ⚠️ 慢" if record.duration_ms > slow_threshold_ms else ""
            lines.append(f"{prefix}{record.name:<30} {record.duration_ms:>10.0f} {pct:>7.1f}%{warn}")
            for child in record.children:
                _format_record(child, indent + 1)

        for record in self.records.values():
            _format_record(record)

        lines.append("=" * 70)
        slow = [n for n, r in self.records.items() if r.duration_ms > slow_threshold_ms]
        if slow:
            names = ", ".join(f"{n} ({self.records[n].duration_ms:.0f}ms)" for n in slow)
            lines.append(f"⚠️ 慢步骤 (>{slow_threshold_ms}ms): {names}")
            lines.append("=" * 70)

        return "\n".join(lines)

    def log_summary(self, slow_threshold_ms: int = 1000):
        """输出到日志"""
        report = self.format_report(slow_threshold_ms)
        logger.info(report)
