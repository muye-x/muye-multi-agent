"""使用 SDK ReAct 模式的旅行参考 Agent。"""

from __future__ import annotations

from langchain_core.tools import tool

from muye_multi_agent_sdk import AgentMetadata, ReActAgent


@tool("sample_itinerary")
def sample_itinerary(city: str, days: int = 3) -> dict[str, object]:
    """生成确定性示例行程，不调用旅行供应商或写入业务数据。"""
    normalized_city = city.strip() or "目的地"
    normalized_days = min(max(days, 1), 14)
    return {
        "markdown": f"已生成 {normalized_city} {normalized_days} 天示例行程。",
        "json_data": {
            "city": normalized_city,
            "days": normalized_days,
            "itinerary": [
                {"day": day, "theme": f"{normalized_city} 第{day}天：城市漫游"}
                for day in range(1, normalized_days + 1)
            ],
        },
    }


class TravelAgent(ReActAgent):
    """仅使用确定性旅行工具的 ReAct 参考服务。"""

    @property
    def langchain_tools(self) -> list:
        return [sample_itinerary]

    @property
    def instructions(self) -> str:
        return (
            "你是旅行规划参考 Agent。只能使用 sample_itinerary 工具生成示例行程，"
            "不得声称已预订、查询价格或访问真实供应商。输出使用中文，并明确结果是示例。"
        )

    @property
    def intent_guard_business_rules(self) -> str:
        """声明旅行场景中可作为上一轮追问答案的单字段补充。"""
        return "旅行任务中，目的地、出发地、出行日期、天数、人数、预算和偏好等单项补充，若能回答最近追问，应视为有效续接输入。"

    @property
    def metadata(self) -> AgentMetadata:
        return AgentMetadata(
            name="travel-agent",
            version="3.0.0",
            description="仅生成确定性示例行程的旅行参考服务",
            supported_intents=["旅行规划"],
        )
