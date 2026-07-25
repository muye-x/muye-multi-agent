"""确定性 Graph 参考 Agent，不代表真实下单能力。"""

from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from muye_multi_agent_sdk import AgentMetadata, AgentRequest, AgentResult, GraphAgent


class OrderState(TypedDict, total=False):
    """订单示例图在节点间传递的最小状态。"""

    task: str
    normalized_task: str


async def normalize_order_task(state: OrderState) -> dict[str, str]:
    """规范化订单示例任务，不执行外部 I/O 或业务写入。"""
    return {"normalized_task": state["task"].strip()}


class OrderGraphAgent(GraphAgent):
    """通过两节点状态图展示 Graph 执行、进度与取消协议。"""

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name="order-agent",
            version="3.0.0",
            description="确定性订单 Graph 参考服务",
            supported_intents=["订单流程演示"],
        )

    def build_graph(self) -> Any:
        graph = StateGraph(OrderState)
        graph.add_node("normalize", normalize_order_task)
        graph.add_edge(START, "normalize")
        graph.add_edge("normalize", END)
        return graph

    async def result_from_state(self, final_state: dict[str, Any], request: AgentRequest) -> AgentResult:
        return AgentResult.success(
            {
                "markdown": "订单 Graph 示例已完成。",
                "json_data": {"task": final_state.get("normalized_task", "")},
            },
            prompt_data="说明这是示例，不执行真实下单。",
            trace_id=request.context.trace_id,
        )

    def node_progress(self, node_name: str) -> tuple[str, int]:
        return ("规范化示例任务", 100) if node_name == "normalize" else super().node_progress(node_name)
