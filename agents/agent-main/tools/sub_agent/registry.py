"""子 Agent 描述符与注册表。"""
from __future__ import annotations
import os
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class SubAgentDescriptor:
    """一个可信内部子服务的固定调用配置。"""
    name: str
    url: str
    timeout_seconds: float
    profile: str = "internal"

class SubAgentRegistry:
    """只保存显式配置的服务，避免模型控制任意 URL。"""
    def __init__(self, descriptors: list[SubAgentDescriptor]) -> None:
        self._descriptors = {descriptor.name: descriptor for descriptor in descriptors}
    def get(self, name: str) -> SubAgentDescriptor:
        try:
            return self._descriptors[name]
        except KeyError as exc:
            raise ValueError(f"未注册子 Agent: {name}") from exc
    def values(self) -> tuple[SubAgentDescriptor, ...]:
        return tuple(self._descriptors.values())

def build_default_registry() -> SubAgentRegistry:
    """从 Muye 环境变量构建 travel 与 order 的内部注册表。"""
    timeout = float(os.getenv("MUYE_AGENT_SUB_AGENT_TIMEOUT", "20"))
    return SubAgentRegistry([
        SubAgentDescriptor("travel", os.getenv("MUYE_AGENT_TRAVEL_URL", "http://127.0.0.1:8011"), timeout),
        SubAgentDescriptor("order", os.getenv("MUYE_AGENT_ORDER_URL", "http://127.0.0.1:8012"), timeout),
    ])
