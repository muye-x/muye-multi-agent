"""主 Agent 的 Block Stream V2 事件投影。

SDK ``SseEmitter`` 负责统一信封、序列、时钟和 block 计数；本模块只保留主服务
特有的 block/tool payload 以及短期事件历史，不承担通用 SSE transport 职责。
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Any

from muye_multi_agent_sdk.transport.sse import SseEmitter


class EventType(Enum):
    """Block Stream V2 的稳定事件集合。"""

    SESSION_START = "session_start"
    SESSION_END = "session_end"
    BLOCK = "block"
    THINKING = "thinking"
    TOOL = "tool"
    ERROR = "error"
    DONE = "done"


class BlockType(Enum):
    """主服务支持的内容 block 类型。"""

    MARKDOWN = "markdown"
    CHART = "chart"
    TABLE = "table"
    CODE = "code"
    IMAGE = "image"
    JSON = "json"


class ToolStatus(Enum):
    """工具事件在一个调用生命周期中的状态。"""

    START = "start"
    RUNNING = "running"
    RESULT = "result"
    COMPLETE = "complete"
    ERROR = "error"


@dataclass(slots=True)
class StreamEvent:
    """Block Stream V2 的结构化 SSE 信封。"""

    event: str
    sessionId: str
    streamId: str
    userId: str
    seq: int
    timestamp: int
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event,
            "sessionId": self.sessionId,
            "streamId": self.streamId,
            "userId": self.userId,
            "seq": self.seq,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    def to_sse(self) -> str:
        return f"event: {self.event}\ndata: {self.to_json()}\n\n"


class EventNormalizer:
    """将主 Agent 运行事件投影为 Block Stream V2。"""

    def __init__(self, session_id: str, stream_id: str, user_id: str | None = None) -> None:
        self._emitter = SseEmitter(
            session_id=session_id,
            stream_id=stream_id,
            user_id=user_id or "unknown",
        )
        self.event_history: deque[StreamEvent] = deque(maxlen=1000)
        self.history_retention = 30_000

    @property
    def session_id(self) -> str:
        return self._emitter.session_id

    @property
    def stream_id(self) -> str:
        return self._emitter.stream_id

    @property
    def user_id(self) -> str:
        return self._emitter.user_id

    @property
    def seq(self) -> int:
        return self._emitter.sequence

    @property
    def event_count(self) -> int:
        return self._emitter.sequence

    @property
    def block_count(self) -> int:
        return self._emitter.block_count

    def _create_event(self, event_type: EventType, data: dict[str, Any]) -> StreamEvent:
        event = StreamEvent(**self._emitter.envelope(event_type.value, data))
        self.event_history.append(event)
        return event

    def session_start(self, model: str | None = None) -> StreamEvent:
        data = {"model": model} if model else {}
        return self._create_event(EventType.SESSION_START, data)

    def session_end(self, total_tokens: int | None = None) -> StreamEvent:
        data: dict[str, Any] = {
            "totalBlocks": self.block_count,
            "duration": self._emitter.duration_ms(),
        }
        if total_tokens is not None:
            data["totalTokens"] = total_tokens
        return self._create_event(EventType.SESSION_END, data)

    def block_delta(
        self,
        block_id: str,
        block_type: BlockType,
        delta: str,
        metadata: dict[str, Any] | None = None,
    ) -> StreamEvent:
        data: dict[str, Any] = {"id": block_id, "type": block_type.value, "delta": delta}
        if metadata:
            data["metadata"] = metadata
        self._emitter.register_block(block_id)
        return self._create_event(EventType.BLOCK, data)

    def block_complete(
        self,
        block_id: str,
        block_type: BlockType,
        content: Any,
        metadata: dict[str, Any] | None = None,
    ) -> StreamEvent:
        data: dict[str, Any] = {"id": block_id, "type": block_type.value, "content": content}
        if metadata:
            data["metadata"] = metadata
        self._emitter.register_block(block_id)
        return self._create_event(EventType.BLOCK, data)

    def thinking(self, thinking_id: str, content: str, collapsed: bool = True) -> StreamEvent:
        return self._create_event(
            EventType.THINKING,
            {"id": thinking_id, "content": content, "collapsed": collapsed},
        )

    def tool_start(
        self,
        tool_id: str,
        tool_name: str,
        tool_input: dict[str, Any] | None = None,
    ) -> StreamEvent:
        data: dict[str, Any] = {"id": tool_id, "name": tool_name, "status": ToolStatus.START.value}
        if tool_input:
            data["input"] = tool_input
        return self._create_event(EventType.TOOL, data)

    def tool_running(
        self,
        tool_id: str,
        tool_name: str,
        log: str | None = None,
        progress: int | None = None,
    ) -> StreamEvent:
        data: dict[str, Any] = {"id": tool_id, "name": tool_name, "status": ToolStatus.RUNNING.value}
        if log:
            data["log"] = log
        if progress is not None:
            data["progress"] = progress
        return self._create_event(EventType.TOOL, data)

    def tool_result(self, tool_id: str, tool_name: str, blocks: list[dict[str, Any]]) -> StreamEvent:
        return self._create_event(
            EventType.TOOL,
            {"id": tool_id, "name": tool_name, "status": ToolStatus.RESULT.value, "blocks": blocks},
        )

    def tool_complete(self, tool_id: str, tool_name: str, duration: int | None = None) -> StreamEvent:
        data: dict[str, Any] = {"id": tool_id, "name": tool_name, "status": ToolStatus.COMPLETE.value}
        if duration is not None:
            data["duration"] = duration
        return self._create_event(EventType.TOOL, data)

    def tool_error(
        self,
        tool_id: str,
        tool_name: str,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> StreamEvent:
        error: dict[str, Any] = {"code": error_code, "message": error_message}
        if error_details:
            error["details"] = error_details
        return self._create_event(
            EventType.TOOL,
            {"id": tool_id, "name": tool_name, "status": ToolStatus.ERROR.value, "error": error},
        )

    def error(
        self,
        error_code: str,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> StreamEvent:
        data: dict[str, Any] = {"code": error_code, "message": error_message}
        if error_details:
            data["details"] = error_details
        return self._create_event(EventType.ERROR, data)

    def done(self) -> StreamEvent:
        return self._create_event(
            EventType.DONE,
            {
                "totalBlocks": self.block_count,
                "totalEvents": self.event_count,
                "duration": self._emitter.duration_ms(),
            },
        )

    def get_events_since(self, seq: int) -> list[StreamEvent]:
        """返回保留窗口内序号大于 ``seq`` 的事件。"""
        current_time = int(time.time() * 1000)
        return [
            event
            for event in self.event_history
            if event.seq > seq and current_time - event.timestamp <= self.history_retention
        ]
