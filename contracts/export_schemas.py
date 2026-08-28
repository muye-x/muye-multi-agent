"""将 Pydantic 契约确定性导出为仓库中的 JSON Schema。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .models import CONTRACT_SCHEMA_MODELS
from .v3 import V3_CONTRACT_SCHEMA_MODELS


def render_schemas() -> dict[str, str]:
    """在内存中生成全部 v2/v3 JSON Schema 的规范文本。"""

    rendered: dict[str, str] = {}
    for schema_name, model in {**CONTRACT_SCHEMA_MODELS, **V3_CONTRACT_SCHEMA_MODELS}.items():
        schema = model.model_json_schema()
        schema["$id"] = f"https://muye.ai/schemas/{schema_name}.schema.json"
        rendered[f"{schema_name}.schema.json"] = (
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
    return rendered


def export_schemas(output_directory: Path) -> None:
    """导出全部 v2/v3 契约 Schema，供非 Python 调用方消费。"""

    output_directory.mkdir(parents=True, exist_ok=True)
    for filename, content in render_schemas().items():
        (output_directory / filename).write_text(content, encoding="utf-8")


def check_schemas(output_directory: Path) -> list[str]:
    """返回已提交 Schema 与当前模型之间的完整文件集合和内容差异。"""

    expected = render_schemas()
    actual_paths = {path.name: path for path in output_directory.glob("*.schema.json")}
    missing_filenames = sorted(expected.keys() - actual_paths.keys())
    issues = [f"缺少 Schema：{filename}" for filename in missing_filenames]
    issues.extend(
        f"存在未声明 Schema：{filename}"
        for filename in sorted(actual_paths.keys() - expected.keys())
    )
    for filename in sorted(expected.keys() & actual_paths.keys()):
        if actual_paths[filename].read_text(encoding="utf-8") != expected[filename]:
            issues.append(f"Schema 内容已漂移：{filename}")
    return issues


def main() -> None:
    """导出 Schema，或以无副作用模式校验受版本控制的 Schema。"""

    parser = argparse.ArgumentParser(description="导出或校验 Muye JSON Schema")
    parser.add_argument("--check", action="store_true", help="仅校验已提交 Schema，不写文件")
    arguments = parser.parse_args()
    output_directory = Path(__file__).with_name("schemas")
    if arguments.check:
        issues = check_schemas(output_directory)
        if issues:
            parser.exit(1, "\n".join(issues) + "\n")
        return
    export_schemas(output_directory)


if __name__ == "__main__":
    main()
