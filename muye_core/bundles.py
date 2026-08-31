"""阶段 2 声明式 Runtime Bundle 生成与校验。"""

from __future__ import annotations

from collections.abc import Mapping
import json

from contracts.v3 import (
    AgentRevisionBundleManifestV1,
    AgentRevisionSpecV1,
    RuntimeResourceBindingV1,
    revision_bundle_checksum,
    revision_spec_checksum,
)

from .service import DomainError


def build_bundle(
    *,
    spec: AgentRevisionSpecV1,
    build_id: str,
    resources: list[RuntimeResourceBindingV1],
    evaluation_summary: Mapping[str, object],
) -> tuple[AgentRevisionBundleManifestV1, dict[str, bytes]]:
    """从已通过门禁的逻辑输入构造可由 Runtime 复核的 Bundle 成员。

    本函数不压缩或写入文件，调用方可使用其成员创建任意确定性传输格式。评测摘要
    必须明确 ``passed=true``，否则不会生成可部署 Bundle。
    """

    if evaluation_summary.get("passed") is not True:
        raise DomainError("EVALUATION_FAILED", "评测未通过，不能生成 Bundle")
    if not resources:
        raise DomainError("VALIDATION_ERROR", "Bundle 至少需要一个知识资源", status_code=422)
    members = {
        "revision.json": _canonical_json(spec.model_dump(mode="json")),
        "resource-snapshot.json": _canonical_json({"resources": [resource.model_dump(mode="json") for resource in resources]}),
        "evaluation-summary.json": _canonical_json(dict(evaluation_summary)),
    }
    revision_checksum = revision_spec_checksum(spec)
    provisional = AgentRevisionBundleManifestV1(
        schema_version="muye.ai/agent-revision-bundle/v1",
        agent_id=spec.agent_id,
        revision_id=spec.revision_id,
        revision_checksum=revision_checksum,
        bundle_checksum="0" * 64,
        build_id=build_id,
        runtime_contract_version="muye-runtime/1",
        resources=resources,
    )
    manifest = provisional.model_copy(update={"bundle_checksum": revision_bundle_checksum(provisional, members)})
    return manifest, members


def verify_bundle(manifest: AgentRevisionBundleManifestV1, members: Mapping[str, bytes]) -> None:
    """在读取成员前验证 Bundle 的内容 checksum。"""

    actual = revision_bundle_checksum(manifest, members)
    if actual != manifest.bundle_checksum:
        raise DomainError("VALIDATION_ERROR", "Bundle checksum 校验失败", status_code=422)


def _canonical_json(value: object) -> bytes:
    """以跨进程稳定 JSON 编码固定 Bundle 成员内容。"""

    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
