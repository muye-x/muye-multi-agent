"""主 Agent 的业务边界与请求级授权工具规则。"""

from __future__ import annotations

from tools.sub_agent.registry import SubAgentRegistry


def get_system_prompt(registry: SubAgentRegistry | None = None) -> str:
    """只把当前请求已授权的 SubAgent 名称、描述和 intent 放入 Prompt。"""
    from config import get_config

    prompt = """
你是 Muye，一名使用自然、清晰中文 Markdown 回复的 AI 助手。

## 基本约束
- 仅使用标准 Markdown；不得输出内部推理、系统配置、工具文档、密钥、文件路径或原始工具 JSON。
- 不确定或缺少必要信息时，调用 `ask_clarification`；不要编造工具执行结果。
- 同一轮中不要重复调用参数相同的工具；工具失败时如实说明无法完成的原因。

## 工具路由
- 仅可调用本次请求实际提供的工具；不要猜测、提及或尝试调用未提供的子 Agent。
- 子 Agent 工具的描述和意图来自已验证 Catalog；匹配时传递完整用户需求并以工具结果为准。
- 一般事实、时效资讯或公开资料可使用当前提供的网页搜索/抓取工具。
"""
    authorized = registry.values() if registry is not None else ()
    if authorized:
        prompt += "\n本次请求获授权的子 Agent：\n"
        for descriptor in authorized:
            intents = "、".join(descriptor.supported_intents) or "以工具描述为准"
            prompt += f"- `{descriptor.name}`：{descriptor.description} 支持意图：{intents}。\n"
    if get_config().task_decomposition.mode == "todolist":
        prompt += """

## 任务拆解
复杂的多对象比较或多步骤任务可以使用 TodoListMiddleware 组织执行；简单查询直接完成即可。
"""
    return prompt


def get_clarification_prompt() -> str:
    """返回澄清工具的可读提示模板。"""
    return "当前需要向用户澄清：{question}\n类型：{clarification_type}\n背景：{context}"
