"""
记忆提取提示词模板
用于指导 LLM 从对话中提取结构化记忆
采用 deer-flow 的提示词设计
"""
from typing import List, Dict, Any
import re

# 记忆提取的主提示词（中文版）
MEMORY_EXTRACTION_PROMPT = """你是一个记忆管理系统。你的任务是分析对话并更新用户的记忆画像。

当前记忆状态：
<current_memory>
{current_memory}
</current_memory>

需要处理的新对话：
<conversation>
{conversation}
</conversation>

指令：
1. 分析对话中关于用户的重要信息
2. 提取相关的事实、偏好和上下文，包含具体细节（数字、名称、技术栈）
3. 根据下面的详细长度指南更新记忆各部分

在提取事实之前，对对话进行结构化反思：
1. 错误/重试检测：Agent 是否遇到错误、需要重试或产生了错误结果？
   如果是，将根本原因和正确方法记录为高置信度事实，类别为 "correction"。
2. 用户纠正检测：用户是否纠正了 Agent 的方向、理解或输出？
   如果是，将正确的解释或方法记录为高置信度事实，类别为 "correction"。
   仅当类别为 "correction" 且对话中明确提到错误时，才在 "sourceError" 中包含出错原因。
3. 项目约束发现：对话中是否发现了项目特定的约束条件？
   如果是，将其记录为事实，使用最合适的类别和置信度。

{correction_hint}

记忆部分指南：

**用户上下文**（当前状态 - 简洁摘要）：
- workContext: 职业角色、公司、关键项目、主要技术栈（2-3 句话）
  示例：核心贡献者，项目名称及指标（16k+ stars），技术栈
- personalContext: 语言能力、沟通偏好、关键兴趣（1-2 句话）
  示例：双语能力、特定兴趣领域、专业领域
- topOfMind: 多个正在进行的关注领域和优先事项（3-5 句话，详细段落）
  示例：主要项目工作、并行技术调研、持续学习/跟踪
  包括：活跃的实现工作、故障排查问题、市场/研究兴趣
  注意：这里捕获多个并发的关注领域，而不仅仅是一个任务

**历史**（时间上下文 - 丰富段落）：
- recentMonths: 近期活动的详细摘要（4-6 句话或 1-2 段）
  时间线：最近 1-3 个月的交互
  包括：探索的技术、参与的项目、解决的问题、展示的兴趣
- earlierContext: 重要的历史模式（3-5 句话或 1 段）
  时间线：3-12 个月前
  包括：过去的项目、学习历程、已建立的模式
- longTermBackground: 持久的背景和基础上下文（2-4 句话）
  时间线：整体/基础信息
  包括：核心专长、长期兴趣、基本工作风格

**事实提取**：
- 提取具体的、可量化的细节（例如："16k+ GitHub stars"、"200+ 数据集"）
- 包含专有名词（公司名、项目名、技术名）
- 保留技术术语和版本号
- 类别：
  * preference: 用户偏好/不喜欢的工具、风格、方法
  * knowledge: 具体专长、掌握的技术、领域知识
  * context: 背景事实（职位、项目、地点、语言）
  * behavior: 工作模式、沟通习惯、解决问题的方法
  * goal: 明确的目标、学习目标、项目抱负
  * correction: 明确的 Agent 错误或用户纠正，包括正确方法
- 置信度级别：
  * 0.9-1.0: 明确陈述的事实（"我在做 X"、"我的角色是 Y"）
  * 0.7-0.8: 从行动/讨论中强烈暗示
  * 0.5-0.6: 推断的模式（谨慎使用，仅用于清晰的模式）

**内容归属**：
- workContext: 当前工作、活跃项目、主要技术栈
- personalContext: 语言、性格、直接工作任务之外的兴趣
- topOfMind: 用户最近关心的多个正在进行的优先事项和关注领域（更新最频繁）
  应捕获 3-5 个并发主题：主要工作、侧面探索、学习/跟踪兴趣
- recentMonths: 近期技术探索和工作的详细记录
- earlierContext: 稍早交互中仍然相关的模式
- longTermBackground: 关于用户的不变基础事实

**多语言内容**：
- 保留专有名词和公司名的原始语言
- 保持技术术语的原始形式（DeepSeek、LangGraph 等）
- 在 personalContext 中注明语言能力

输出格式（JSON）：
{{
  "user": {{
    "workContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "personalContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "topOfMind": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "history": {{
    "recentMonths": {{ "summary": "...", "shouldUpdate": true/false }},
    "earlierContext": {{ "summary": "...", "shouldUpdate": true/false }},
    "longTermBackground": {{ "summary": "...", "shouldUpdate": true/false }}
  }},
  "newFacts": [
    {{ "content": "...", "category": "preference|knowledge|context|behavior|goal|correction", "confidence": 0.0-1.0, "sourceError": "..." }}
  ],
  "factsToRemove": ["fact_id_1", "fact_id_2"]
}}

重要规则：
- 仅当有有意义的新信息时才设置 shouldUpdate=true
- 遵循长度指南：workContext/personalContext 简洁（1-3 句话），topOfMind 和 history 部分详细（段落）
- 包含具体指标、版本号和专有名词
- 对于 correction 类别的事实，在 sourceError 字段中描述出错原因
- 通过 factsToRemove 删除过时或矛盾的事实
- 保留多语言内容和技术术语
- **所有 summary 字段的内容必须使用中文书写**（技术术语和专有名词保持原文）
"""


def format_conversation_for_extraction(messages: List[Any]) -> str:
    """
    将消息列表格式化为适合提取的文本（采用 deer-flow 的格式化逻辑）

    Args:
        messages: 消息列表（支持字典或对象格式）

    Returns:
        str: 格式化后的对话文本
    """
    from config.settings import get_config
    config = get_config()
    max_length = config.content_processing.memory_content_max_length

    lines = []
    for msg in messages:
        # 支持字典格式（从 Redis）和对象格式（从 LangChain）
        if isinstance(msg, dict):
            role = msg.get("role", "unknown")
            content = msg.get("content", str(msg))
        else:
            role = getattr(msg, "type", "unknown")
            content = getattr(msg, "content", str(msg))

        # 处理可能为 list 的多模态内容
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if isinstance(p, str):
                    text_parts.append(p)
                elif isinstance(p, dict):
                    text_val = p.get("text")
                    if isinstance(text_val, str):
                        text_parts.append(text_val)
            content = " ".join(text_parts) if text_parts else str(content)

        # 删除用户消息中的 uploaded_files 标签，避免将临时文件路径写入长期记忆
        if role in ("human", "user"):
            content = re.sub(r"<uploaded_files>[\s\S]*?</uploaded_files>\n*", "", str(content)).strip()
            if not content:
                continue

        # 截断过长消息
        if len(str(content)) > max_length:
            content = str(content)[:max_length] + "..."

        if role in ("human", "user"):
            lines.append(f"User: {content}")
        elif role in ("ai", "assistant"):
            lines.append(f"Assistant: {content}")

    return "\n\n".join(lines)


def format_memory_for_injection(
    user_context: Dict[str, Any],
    facts: List[Dict[str, Any]],
    max_tokens: int = 2000
) -> str:
    """
    将记忆格式化为适合注入到提示词的文本
    使用优先级策略控制 token 数量

    Args:
        user_context: 用户上下文（包含 user 和 history）
        facts: 事实列表
        max_tokens: 最大 token 数（粗略估算，1 token ≈ 1.5 字符）

    Returns:
        str: 格式化后的记忆文本
    """
    from config import get_config

    config = get_config().memory

    # 计算各优先级的 token 预算
    high_budget = int(max_tokens * config.priority_high_weight)
    medium_budget = int(max_tokens * config.priority_medium_weight)
    low_budget = int(max_tokens * config.priority_low_weight)

    sections = []

    # === 高优先级：topOfMind + 纠正类事实 ===
    high_priority_parts = []

    # 用户当前关注（topOfMind）
    top_of_mind = user_context.get("user", {}).get("topOfMind", {}).get("summary", "")
    if top_of_mind:
        high_priority_parts.append(f"## 当前关注\n{top_of_mind}")

    # 纠正类事实
    correction_facts = [f for f in facts if f.get("category") == "correction"]
    if correction_facts:
        facts_text = "\n".join([f"- {f.get('content')}" for f in correction_facts[:5]])
        high_priority_parts.append(f"## 需要避免的错误\n{facts_text}")

    high_priority_text = "\n\n".join(high_priority_parts)
    high_priority_text = _truncate_text(high_priority_text, high_budget)
    if high_priority_text:
        sections.append(high_priority_text)

    # === 中优先级：workContext + 一般事实 ===
    medium_priority_parts = []

    # 工作上下文（workContext）
    work_context = user_context.get("user", {}).get("workContext", {}).get("summary", "")
    if work_context:
        medium_priority_parts.append(f"## 工作上下文\n{work_context}")

    # 偏好和行为类事实
    pref_facts = [f for f in facts if f.get("category") in ["preference", "behavior"]]
    if pref_facts:
        facts_text = "\n".join([f"- {f.get('content')}" for f in pref_facts[:10]])
        medium_priority_parts.append(f"## 偏好和习惯\n{facts_text}")

    medium_priority_text = "\n\n".join(medium_priority_parts)
    medium_priority_text = _truncate_text(medium_priority_text, medium_budget)
    if medium_priority_text:
        sections.append(medium_priority_text)

    # === 低优先级：history + 背景信息 ===
    low_priority_parts = []

    # 个人背景（personalContext）
    personal_context = user_context.get("user", {}).get("personalContext", {}).get("summary", "")
    if personal_context:
        low_priority_parts.append(f"## 个人背景\n{personal_context}")

    # 近期活动（recentMonths）
    recent_months = user_context.get("history", {}).get("recentMonths", {}).get("summary", "")
    if recent_months:
        low_priority_parts.append(f"## 近期活动\n{recent_months}")

    low_priority_text = "\n\n".join(low_priority_parts)
    low_priority_text = _truncate_text(low_priority_text, low_budget)
    if low_priority_text:
        sections.append(low_priority_text)

    # 组合所有部分
    if not sections:
        return ""

    return "# 用户记忆\n\n" + "\n\n".join(sections)


def _truncate_text(text: str, max_tokens: int) -> str:
    """
    截断文本以适应 token 限制
    粗略估算：1 token ≈ 1.5 字符（中文）或 4 字符（英文）

    Args:
        text: 原始文本
        max_tokens: 最大 token 数

    Returns:
        str: 截断后的文本
    """
    # 粗略估算：平均 1 token = 2 字符
    max_chars = max_tokens * 2

    if len(text) <= max_chars:
        return text

    # 截断并添加省略号
    return text[:max_chars - 3] + "..."
