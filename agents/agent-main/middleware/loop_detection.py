"""循环检测中间件 - 防止 Agent 重复调用相同工具导致死循环

P0 安全级别：防止 Agent 无限递归直到达到递归限制

检测策略：
  1. 每次模型响应后，对工具调用（名称 + 参数）进行哈希
  2. 在滑动窗口中跟踪最近的哈希值
  3. 如果相同哈希出现 >= warn_threshold 次，注入警告消息（每个哈希仅一次）
  4. 如果出现 >= hard_limit 次，强制清空 tool_calls，迫使 Agent 输出最终答案
"""

import hashlib
import json
import logging
import threading
from collections import OrderedDict, defaultdict
from typing import Optional, Dict, Any

from .base import AgentMiddleware

logger = logging.getLogger(__name__)

# 默认配置 - 可通过构造函数覆盖
_DEFAULT_WARN_THRESHOLD = 3  # 3 次相同调用后注入警告
_DEFAULT_HARD_LIMIT = 5  # 5 次相同调用后强制停止
_DEFAULT_WINDOW_SIZE = 20  # 跟踪最近 N 次工具调用
_DEFAULT_MAX_TRACKED_THREADS = 100  # LRU 淘汰限制


def _hash_tool_calls(tool_calls: list) -> str:
    """对工具调用集合（名称 + 参数）进行确定性哈希

    设计为顺序无关：相同的工具调用多重集应始终产生相同的哈希值
    """
    # 首先将每个工具调用规范化为最小的 (name, args) 结构
    normalized = []
    for tc in tool_calls:
        normalized.append({
            "name": tc.get("name", ""),
            "args": tc.get("args", {}),
        })

    # 按名称和参数的确定性序列化排序
    normalized.sort(
        key=lambda tc: (
            tc["name"],
            json.dumps(tc["args"], sort_keys=True, default=str),
        )
    )
    blob = json.dumps(normalized, sort_keys=True, default=str)
    return hashlib.md5(blob.encode()).hexdigest()[:12]


_WARNING_MSG = (
    "[循环检测] 你正在重复相同的工具调用。"
    "请停止调用工具并立即给出最终答案。"
    "如果无法完成任务，请总结目前已完成的工作。"
)

_HARD_STOP_MSG = (
    "[强制停止] 重复工具调用超过安全限制。"
    "使用目前收集的结果生成最终答案。"
)


class LoopDetectionMiddleware(AgentMiddleware):
    """检测并打破重复的工具调用循环

    Args:
        warn_threshold: 注入警告消息前的相同工具调用集合次数。默认: 3
        hard_limit: 完全清空 tool_calls 前的相同工具调用集合次数。默认: 5
        window_size: 跟踪调用的滑动窗口大小。默认: 20
        max_tracked_threads: 淘汰最少使用线程前的最大跟踪线程数。默认: 100
    """

    def __init__(
        self,
        warn_threshold: int = _DEFAULT_WARN_THRESHOLD,
        hard_limit: int = _DEFAULT_HARD_LIMIT,
        window_size: int = _DEFAULT_WINDOW_SIZE,
        max_tracked_threads: int = _DEFAULT_MAX_TRACKED_THREADS,
    ):
        super().__init__()
        self.warn_threshold = warn_threshold
        self.hard_limit = hard_limit
        self.window_size = window_size
        self.max_tracked_threads = max_tracked_threads
        self._lock = threading.Lock()
        # 使用 OrderedDict 进行 LRU 淘汰的每线程跟踪
        self._history: OrderedDict[str, list[str]] = OrderedDict()
        self._warned: dict[str, set[str]] = defaultdict(set)

    def _get_thread_id(self, runtime) -> str:
        """从 runtime 上下文提取 thread_id 用于每线程跟踪"""
        thread_id = runtime.context.get("thread_id") if runtime and runtime.context else None
        if thread_id:
            return str(thread_id)
        return "default"

    def _evict_if_needed(self) -> None:
        """如果超过限制则淘汰最少使用的线程

        必须在持有 self._lock 时调用
        """
        while len(self._history) > self.max_tracked_threads:
            evicted_id, _ = self._history.popitem(last=False)
            self._warned.pop(evicted_id, None)
            logger.debug("淘汰线程 %s 的循环跟踪 (LRU)", evicted_id)

    def _track_and_check(self, state, runtime) -> tuple[Optional[str], bool]:
        """跟踪工具调用并检查循环

        Returns:
            (warning_message_or_none, should_hard_stop)
        """
        messages = state.get("messages", [])
        if not messages:
            return None, False

        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None, False

        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None, False

        thread_id = self._get_thread_id(runtime)
        call_hash = _hash_tool_calls(tool_calls)

        with self._lock:
            # 触碰/创建条目（移到末尾用于 LRU）
            if thread_id in self._history:
                self._history.move_to_end(thread_id)
            else:
                self._history[thread_id] = []
                self._evict_if_needed()

            history = self._history[thread_id]
            history.append(call_hash)

            # 保持窗口大小
            if len(history) > self.window_size:
                history.pop(0)

            # 计数此哈希在窗口中出现的次数
            count = history.count(call_hash)

            # 硬停止检查
            if count >= self.hard_limit:
                logger.warning(
                    "线程 %s 的循环硬停止：哈希 %s 出现 %d 次",
                    thread_id, call_hash, count
                )
                return None, True

            # 警告检查（每个哈希仅警告一次）
            if count >= self.warn_threshold:
                warned_set = self._warned[thread_id]
                if call_hash not in warned_set:
                    warned_set.add(call_hash)
                    logger.warning(
                        "线程 %s 的循环警告：哈希 %s 出现 %d 次",
                        thread_id, call_hash, count
                    )
                    return _WARNING_MSG, False

        return None, False

    def _apply(self, state, runtime) -> Optional[Dict[str, Any]]:
        """应用循环检测逻辑"""
        warning, hard_stop = self._track_and_check(state, runtime)

        if hard_stop:
            # 从最后一条 AIMessage 中清空 tool_calls 以强制文本输出
            messages = state.get("messages", [])
            last_msg = messages[-1]
            stripped_msg = last_msg.model_copy(update={
                "tool_calls": [],
                "content": (last_msg.content or "") + f"\n\n{_HARD_STOP_MSG}",
            })
            return {"messages": [stripped_msg]}

        if warning:
            # 注入为 HumanMessage 而不是 SystemMessage 以避免
            # Anthropic 的"多个非连续系统消息"错误
            from langchain_core.messages import HumanMessage
            return {"messages": [HumanMessage(content=warning)]}

        return None

    def after_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        return self._apply(state, runtime)

    async def aafter_model(self, state, runtime) -> Optional[Dict[str, Any]]:
        return self._apply(state, runtime)

    def reset(self, thread_id: Optional[str] = None) -> None:
        """清除跟踪状态。如果给定 thread_id，仅清除该线程"""
        with self._lock:
            if thread_id:
                self._history.pop(thread_id, None)
                self._warned.pop(thread_id, None)
            else:
                self._history.clear()
                self._warned.clear()
