"""阶段 2 Revision 冻结规则。

本模块只负责把可编辑 Draft 与内容寻址资料投影为不可变的 v3 契约，不访问数据库或
Artifact。仓储在同一事务内读取 Draft 和 Asset 后调用它，确保 PostgreSQL 与测试
仓储使用完全一致的 checksum、资料漂移和评测引用校验。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from contracts.v3 import AgentRevisionSpecV1, revision_spec_checksum

from .service import DomainError


def freeze_revision_spec(
    *,
    agent_id: str,
    revision_id: str,
    revision_number: int,
    draft_config: Mapping[str, object],
    sources: Sequence[Mapping[str, object]],
) -> tuple[AgentRevisionSpecV1, str]:
    """构造并校验不可变 Revision。

    ``draft_config`` 必须恰好是 ``AgentRevisionSpecV1`` 去除身份和资料后的字段；
    identity 与资料均只能由 Core 生成。任一无效的 Prompt、预算、模型配置或评测
    引用都会被转换成稳定的公开校验错误，绝不写入 Revision 表。
    """

    payload = {
        **dict(draft_config),
        "schema_version": "muye.ai/agent-revision/v1",
        "agent_id": agent_id,
        "revision_id": revision_id,
        "revision_number": revision_number,
        "source_assets": [dict(source) for source in sources],
    }
    try:
        spec = AgentRevisionSpecV1.model_validate(payload)
    except ValidationError as exc:
        raise DomainError("VALIDATION_ERROR", "Draft 不符合 Revision 契约", status_code=422) from exc
    return spec, revision_spec_checksum(spec)
