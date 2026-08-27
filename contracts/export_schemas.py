"""将 Pydantic 契约确定性导出为仓库中的 JSON Schema。"""

from __future__ import annotations

import json
from pathlib import Path

from .models import CONTRACT_SCHEMA_MODELS
from .v3 import V3_CONTRACT_SCHEMA_MODELS


def export_schemas(output_directory: Path) -> None:
    """导出全部 v2.0 契约 Schema，供非 Python 调用方和 CI 消费。"""
    output_directory.mkdir(parents=True, exist_ok=True)
    for schema_name, model in {**CONTRACT_SCHEMA_MODELS, **V3_CONTRACT_SCHEMA_MODELS}.items():
        schema = model.model_json_schema()
        schema["$id"] = f"https://muye.ai/schemas/{schema_name}.schema.json"
        (output_directory / f"{schema_name}.schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> None:
    """按固定路径写入受版本控制的 JSON Schema。"""
    export_schemas(Path(__file__).with_name("schemas"))


if __name__ == "__main__":
    main()
