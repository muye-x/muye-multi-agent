"""阶段 1 PoC 的本地命令行入口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .contracts import Phase1PocConfigV1
from .generator import generate_agent_directory
from .markdown import parse_markdown_file
from .profile import build_generation_spec, build_profile, canonical_checksum


def main() -> int:
    """解析受控 Markdown 并创建一次性 Agent 目录，返回可审计的最小报告。"""
    parser = argparse.ArgumentParser(description="Muye v2.0 阶段 1 文档到 Agent 目录 PoC")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--document", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--scope-field", default="knowledge_id")
    parser.add_argument("--scope-value", required=True)
    args = parser.parse_args()

    document = parse_markdown_file(args.document, source_root=args.source_root)
    profile = build_profile(document)
    config = Phase1PocConfigV1(
        schema_version="muye.ai/phase1-poc-config/v1",
        agent_slug=args.slug,
        resource_id=args.resource_id,
        resource_revision="resource/phase1-v1",
        model_alias="chat-default",
        scope_field=args.scope_field,
        scope_value=args.scope_value,
    )
    spec = build_generation_spec(document=document, profile=profile, config=config)
    target = generate_agent_directory(
        target_parent=args.output,
        generation_spec=spec,
        profile=profile,
        document=document,
        config=config,
    )
    report = {
        "document_id": document.document_id,
        "document_checksum": document.content_checksum,
        "profile_checksum": canonical_checksum(profile.model_dump(mode="json")),
        "generation_spec_checksum": spec.input_checksum,
        "generated_directory": str(target),
    }
    sys.stdout.write(json.dumps(report, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
