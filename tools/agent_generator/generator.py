"""v2.0 模板 Agent 的确定性、一次性目录生成与验证实现。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import ctypes
import difflib
import errno
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

from contracts.models import AgentDescriptorV1, AgentGenerationSpecV1, SourceProvenanceV1

from .approvals import assert_approval
from .checksums import PROVENANCE_FILE_NAME, canonical_checksum, read_source_tree, source_tree_checksum
from .io import assert_path_within, load_json_model, load_yaml_model, target_exists, write_json
from .models import (
    AgentProfileInputV1,
    GenerationRecipeV1,
    KnowledgeGenerationInputV1,
    TemplateManifestV1,
)


GENERATOR_VERSION = "2.0.0"
RECIPE_FILE_NAME = ".muye-generation-input.json"
_TEMPLATE_MANIFEST_FILE = "template-manifest.yaml"
_PLACEHOLDER_PATTERN = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
_RENDERED_FILES: dict[str, str] = {
    ".dockerignore": ".dockerignore",
    "Dockerfile": "Dockerfile",
    "README.md.tmpl": "README.md",
    "agent.py.tmpl": "agent.py",
    "agent.yaml.tmpl": "agent.yaml",
    "main.py.tmpl": "main.py",
    "prompts/system.md.tmpl": "prompts/system.md",
    "requirements.txt.tmpl": "requirements.txt",
    "tests/test_contract.py.tmpl": "tests/test_contract.py",
}
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1


@dataclass(frozen=True)
class GeneratorPaths:
    """Generator 可读写边界；CLI 只通过 `for_workspace()` 构造正式路径。"""

    workspace_root: Path
    agents_root: Path
    config_root: Path
    templates_root: Path

    @classmethod
    def for_workspace(cls, workspace_root: Path) -> "GeneratorPaths":
        """构造 Scaffold 标准目录，所有相对用户输入都将在这些边界内解析。"""
        root = workspace_root.resolve(strict=True)
        return cls(
            workspace_root=root,
            agents_root=root / "agents",
            config_root=root / "config",
            templates_root=root / "templates" / "agents",
        )


@dataclass(frozen=True)
class GenerationResult:
    """一次成功生成的目录、descriptor 和 provenance 摘要。"""

    directory: Path
    descriptor: AgentDescriptorV1
    provenance: SourceProvenanceV1


@dataclass(frozen=True)
class ValidationReport:
    """`agent validate` 的可机器读取结果，源码 drift 是提示而非接管后失败。"""

    directory: Path
    descriptor: AgentDescriptorV1
    provenance: SourceProvenanceV1
    source_tree_checksum: str
    source_drift: bool
    missing_generated_files: tuple[str, ...]

    @property
    def is_valid(self) -> bool:
        """保留开发者合法修改的同时，拒绝破损 descriptor/provenance 基线。"""
        return not self.missing_generated_files


@dataclass(frozen=True)
class DiffResult:
    """模板重渲染与当前目录的只读差异。"""

    directory: Path
    has_changes: bool
    text: str


class AgentGenerator:
    """从确认的本地逻辑配置生成、校验和对比模板 Agent。

    本类不连接 Milvus、对象存储或模型服务。它只读取 `config/` 和版本化模板，在
    staging 目录完成所有产物校验后以不覆盖 rename 发布目标目录。
    """

    def __init__(self, paths: GeneratorPaths, *, clock: Callable[[], datetime] | None = None) -> None:
        self._paths = paths
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def generate(self, *, slug: str, knowledge_slug: str) -> GenerationResult:
        """生成一个此前不存在的 `agents/agent-<slug>` 目录。

        输入配置和模板在创建 staging 目录前全部校验；任何失败都会清理本次 staging
        目录，且绝不覆盖目标或开发者已接管的 Agent。
        """
        profile_input, knowledge, recipe, manifest, template_directory = self._load_generation_inputs(
            slug=slug,
            knowledge_slug=knowledge_slug,
        )
        target = self._target_directory(slug)
        self._assert_target_available(target)
        self._ensure_agents_root()

        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=self._paths.agents_root))
        try:
            rendered = self._render_files(recipe=recipe, manifest=manifest, template_directory=template_directory)
            self._write_rendered_files(staging, rendered)
            write_json(staging / RECIPE_FILE_NAME, recipe.model_dump(mode="json"))
            descriptor = self._load_descriptor(staging / "agent.yaml")
            self._assert_rendered_descriptor_matches_recipe(descriptor, recipe)
            self._compile_and_import_generated_agent(staging, descriptor, recipe)
            provenance = self._build_provenance(staging=staging, recipe=recipe, knowledge=knowledge)
            write_json(staging / PROVENANCE_FILE_NAME, provenance.model_dump(mode="json"))
            self._assert_staging_integrity(staging, provenance)
            self._rename_without_overwrite(staging, target)
            return GenerationResult(directory=target, descriptor=descriptor, provenance=provenance)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def validate(self, *, slug: str) -> ValidationReport:
        """校验已生成 Agent 的 descriptor、provenance、文件边界和源码完整性。"""
        directory = self._target_directory(slug)
        self._assert_existing_agent_directory(directory)
        descriptor = self._load_descriptor(directory / "agent.yaml")
        if descriptor.slug != slug:
            raise ValueError("agent.yaml 中的 slug 与目标目录不一致")
        provenance = self._load_provenance(directory / PROVENANCE_FILE_NAME)
        self._assert_descriptor_matches_provenance(descriptor, provenance)
        recipe = load_json_model(directory / RECIPE_FILE_NAME, GenerationRecipeV1)
        self._assert_stable_descriptor_fields(descriptor, recipe)
        self._assert_provenance_matches_recipe(provenance, recipe)
        expected_generated_files = self._expected_generated_files()
        if set(provenance.generated_files) != expected_generated_files:
            raise ValueError("provenance generated_files 与 Generator 固定产物清单不一致")
        actual_files = set(read_source_tree(directory)) | {PROVENANCE_FILE_NAME}
        missing = tuple(sorted(expected_generated_files - actual_files))
        for source_file in (directory / "agent.py", directory / "main.py"):
            if source_file.exists():
                compile(source_file.read_text(encoding="utf-8"), str(source_file), "exec")
        actual_checksum = source_tree_checksum(directory)
        return ValidationReport(
            directory=directory,
            descriptor=descriptor,
            provenance=provenance,
            source_tree_checksum=actual_checksum,
            source_drift=actual_checksum != provenance.generated_source_tree_checksum,
            missing_generated_files=missing,
        )

    def diff(self, *, slug: str, template: str = "latest") -> DiffResult:
        """以 recipe 重渲染模板并返回 unified diff，整个过程不写入工作区。"""
        directory = self._target_directory(slug)
        self._assert_existing_agent_directory(directory)
        recipe = load_json_model(directory / RECIPE_FILE_NAME, GenerationRecipeV1)
        if recipe.generation_spec.slug != slug:
            raise ValueError("generation recipe 中的 slug 与目标目录不一致")
        manifest, template_directory, updated_recipe = self._resolve_diff_template(recipe, template)
        expected_tree = self._render_files(
            recipe=updated_recipe,
            manifest=manifest,
            template_directory=template_directory,
        )
        expected_tree[RECIPE_FILE_NAME] = _stable_json(updated_recipe.model_dump(mode="json"))
        actual_tree = read_source_tree(directory)
        text = _unified_tree_diff(actual_tree, expected_tree)
        return DiffResult(directory=directory, has_changes=bool(text), text=text)

    def _load_generation_inputs(
        self,
        *,
        slug: str,
        knowledge_slug: str,
    ) -> tuple[AgentProfileInputV1, KnowledgeGenerationInputV1, GenerationRecipeV1, TemplateManifestV1, Path]:
        """读取可信配置并一次性组装带自校验 checksum 的正式 GenerationSpec。"""
        _validate_cli_slug(slug, label="Agent slug")
        _validate_cli_slug(knowledge_slug, label="知识 slug")
        profile_path = self._paths.config_root / "agents" / f"{slug}.yaml"
        knowledge_path = self._paths.config_root / "knowledge" / f"{knowledge_slug}.yaml"
        self._assert_config_path(profile_path)
        self._assert_config_path(knowledge_path)
        profile_input = load_yaml_model(profile_path, AgentProfileInputV1)
        knowledge = load_yaml_model(knowledge_path, KnowledgeGenerationInputV1)
        if profile_input.slug != slug:
            raise ValueError("Agent 配置中的 slug 必须与命令参数一致")
        if knowledge.knowledge_slug != knowledge_slug:
            raise ValueError("知识配置中的 knowledge_slug 必须与命令参数一致")
        self._assert_generation_approvals(profile_input=profile_input, knowledge=knowledge)
        manifest, template_directory = self._resolve_template("react-knowledge", template_version=None)
        spec = self._assemble_generation_spec(profile_input=profile_input, knowledge=knowledge, manifest=manifest)
        recipe = GenerationRecipeV1(
            schema_version="muye.ai/generation-recipe/v1",
            generation_spec=spec,
            knowledge=knowledge,
            profile_input=profile_input,
        )
        return profile_input, knowledge, recipe, manifest, template_directory

    def _assemble_generation_spec(
        self,
        *,
        profile_input: AgentProfileInputV1,
        knowledge: KnowledgeGenerationInputV1,
        manifest: TemplateManifestV1,
    ) -> AgentGenerationSpecV1:
        """由确认的 Profile、Resource 和 Skill 组装唯一的模板输入快照。"""
        profile_checksum = canonical_checksum(profile_input.profile.model_dump(mode="json"))
        payload: dict[str, object] = {
            "schema_version": "muye.ai/agent-generation-spec/v1",
            "agent_id": profile_input.agent_id,
            "slug": profile_input.slug,
            "template_id": manifest.template_id,
            "template_version": manifest.template_version,
            "sdk_version": manifest.sdk_version,
            "agent_profile_revision": profile_input.profile_revision,
            "agent_profile_checksum": profile_checksum,
            "resource_id": knowledge.resource_id,
            "resource_revision": knowledge.resource_revision,
            "skill_revision": knowledge.skill_revision,
            "skill_checksum": knowledge.skill_checksum,
            "model_alias": knowledge.model_alias,
            "retrieval_pipeline": knowledge.retrieval_pipeline,
            "scope_filter_ref": knowledge.scope_filter_ref,
            "allowed_filter_fields": knowledge.allowed_filter_fields,
            "allowed_return_fields": knowledge.allowed_return_fields,
            "tool_budget": knowledge.tool_budget,
            "token_budget": knowledge.token_budget,
            "timeout_budget_seconds": knowledge.timeout_budget_seconds,
            "evaluation_set_ref": knowledge.evaluation_set_ref,
        }
        payload["input_checksum"] = canonical_checksum(payload)
        return AgentGenerationSpecV1.model_validate(payload)

    def _resolve_template(
        self,
        template_id: str,
        *,
        template_version: str | None,
    ) -> tuple[TemplateManifestV1, Path]:
        """从受控 templates 根寻找精确版本或最高已发布版本的无 symlink 模板。"""
        templates_root = self._paths.templates_root
        if templates_root.is_symlink() or not templates_root.is_dir():
            raise ValueError("模板根目录不存在、不是目录或是符号链接")
        template_parent = templates_root / template_id
        assert_path_within(template_parent, templates_root, description="模板目录")
        if template_parent.is_symlink() or not template_parent.is_dir():
            raise ValueError(f"模板不存在或不是目录：{template_id}")

        candidates: list[tuple[TemplateManifestV1, Path]] = []
        for directory in sorted(template_parent.iterdir()):
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError(f"模板目录不能包含符号链接或普通文件：{directory}")
            manifest_path = directory / _TEMPLATE_MANIFEST_FILE
            if not manifest_path.is_file() or manifest_path.is_symlink():
                raise ValueError(f"模板缺少普通 manifest 文件：{directory}")
            manifest = load_yaml_model(manifest_path, TemplateManifestV1)
            if manifest.template_id != template_id:
                raise ValueError(f"模板目录与 manifest template_id 不一致：{directory}")
            self._assert_template_tree(directory)
            if template_version is None or manifest.template_version == template_version:
                candidates.append((manifest, directory))
        if not candidates:
            reference = template_version or "latest"
            raise ValueError(f"未找到模板 {template_id}@{reference}")
        if template_version is not None:
            if len(candidates) != 1:
                raise ValueError(f"模板版本不唯一：{template_id}@{template_version}")
            return candidates[0]
        return max(candidates, key=lambda candidate: _semver_sort_key(candidate[0].template_version))

    def _resolve_diff_template(
        self,
        recipe: GenerationRecipeV1,
        template: str,
    ) -> tuple[TemplateManifestV1, Path, GenerationRecipeV1]:
        """为 diff 选择模板；仅 `latest` 或 provenance 的精确版本可用。"""
        if template == "latest":
            manifest, template_directory = self._resolve_template(recipe.generation_spec.template_id, template_version=None)
        elif template == "source":
            manifest, template_directory = self._resolve_template(
                recipe.generation_spec.template_id,
                template_version=recipe.generation_spec.template_version,
            )
        else:
            raise ValueError("--template 仅支持 latest 或 source")
        spec_payload = recipe.generation_spec.model_dump(mode="json")
        spec_payload["template_id"] = manifest.template_id
        spec_payload["template_version"] = manifest.template_version
        spec_payload["sdk_version"] = manifest.sdk_version
        spec_payload.pop("input_checksum")
        spec_payload["input_checksum"] = canonical_checksum(spec_payload)
        updated_recipe = recipe.model_copy(
            update={"generation_spec": AgentGenerationSpecV1.model_validate(spec_payload)}
        )
        return manifest, template_directory, updated_recipe

    def _render_files(
        self,
        *,
        recipe: GenerationRecipeV1,
        manifest: TemplateManifestV1,
        template_directory: Path,
    ) -> dict[str, str]:
        """使用字面量替换渲染固定文件清单，绝不执行任意模板表达式。"""
        expected_template_files = set(_RENDERED_FILES) | {_TEMPLATE_MANIFEST_FILE}
        actual_template_files = {
            path.relative_to(template_directory).as_posix()
            for path in template_directory.rglob("*")
            if path.is_file()
        }
        if actual_template_files != expected_template_files:
            unexpected = sorted(actual_template_files ^ expected_template_files)
            raise ValueError("模板文件清单不受支持：" + ", ".join(unexpected))
        context = self._render_context(recipe=recipe, manifest=manifest)
        rendered: dict[str, str] = {}
        for template_relative_path, output_relative_path in _RENDERED_FILES.items():
            source_path = template_directory / template_relative_path
            content = source_path.read_text(encoding="utf-8")
            if template_relative_path.endswith(".tmpl"):
                content = _render_template(content, context=context, source_path=source_path)
            rendered[output_relative_path] = content
        return rendered

    def _render_context(self, *, recipe: GenerationRecipeV1, manifest: TemplateManifestV1) -> dict[str, str]:
        """仅将经过模型与 checksum 验证的数据编码为 YAML/Python 的字面量。"""
        spec = recipe.generation_spec
        profile = recipe.profile_input.profile
        scope = recipe.knowledge.fixed_scope_filter
        tool_name = _tool_name_for_slug(spec.slug)
        values = {
            "agent_id": _json_literal(spec.agent_id),
            "agent_version": _json_literal(recipe.profile_input.agent_version),
            "class_name": _class_name_for_slug(spec.slug),
            "description": _json_literal(profile.description),
            "display_name": _json_literal(profile.display_name),
            "display_name_markdown": profile.display_name,
            "fixed_scope_filter_json": _json_literal(scope.model_dump(mode="json")),
            "instructions": profile.instructions,
            "model_alias": _json_literal(spec.model_alias),
            "resource_id": _json_literal(spec.resource_id),
            "retrieval_pipeline": _json_literal(spec.retrieval_pipeline),
            "return_fields_json": _json_literal(spec.allowed_return_fields),
            "sdk_version": _json_literal(manifest.sdk_version),
            "skill_ref": _json_literal(spec.skill_revision),
            "slug": _json_literal(spec.slug),
            "slug_markdown": spec.slug,
            "supported_intents_json": _json_literal(profile.supported_intents),
            "timeout_budget_seconds": str(spec.timeout_budget_seconds),
            "tool_name": tool_name,
            "tool_name_retrieve_json": _json_literal(f"{tool_name}_retrieve"),
            "token_budget": str(spec.token_budget),
            "tool_budget": str(spec.tool_budget),
        }
        return values

    def _write_rendered_files(self, staging: Path, rendered: dict[str, str]) -> None:
        """仅在 staging 中创建受允许的相对文件，防止模板路径穿越。"""
        for relative_path, content in rendered.items():
            output_path = staging / relative_path
            assert_path_within(output_path, staging, description="模板输出文件")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(content, encoding="utf-8")

    def _build_provenance(
        self,
        *,
        staging: Path,
        recipe: GenerationRecipeV1,
        knowledge: KnowledgeGenerationInputV1,
    ) -> SourceProvenanceV1:
        """在源码树完整后记录可审计来源；时间不参与 stable tree checksum。"""
        generated_files = sorted(self._expected_generated_files())
        generated_at = self._clock().astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return SourceProvenanceV1(
            schema_version="muye.ai/source-provenance/v1",
            generator_version=GENERATOR_VERSION,
            template_id=recipe.generation_spec.template_id,
            template_version=recipe.generation_spec.template_version,
            sdk_version=recipe.generation_spec.sdk_version,
            generation_spec_checksum=recipe.generation_spec.input_checksum,
            knowledge_resource_checksum=knowledge.resource_checksum,
            skill_checksum=recipe.generation_spec.skill_checksum,
            profile_checksum=recipe.generation_spec.agent_profile_checksum,
            generated_at=generated_at,
            generated_files=generated_files,
            generated_source_tree_checksum=source_tree_checksum(staging),
        )

    def _assert_staging_integrity(self, staging: Path, provenance: SourceProvenanceV1) -> None:
        """写入 provenance 后再次自校验，确保发布前不会留下不完整基线。"""
        actual_files = set(read_source_tree(staging)) | {PROVENANCE_FILE_NAME}
        if actual_files != self._expected_generated_files() or actual_files != set(provenance.generated_files):
            raise ValueError("staging 生成文件清单与 provenance 不一致")
        if source_tree_checksum(staging) != provenance.generated_source_tree_checksum:
            raise ValueError("staging 源码树 checksum 与 provenance 不一致")

    def _target_directory(self, slug: str) -> Path:
        """从严格 slug 唯一派生目标目录，并禁止保留的 MainAgent 名称。"""
        _validate_cli_slug(slug, label="Agent slug")
        if slug == "main":
            raise ValueError("保留 slug main 不能用于生成 SubAgent")
        target = self._paths.agents_root / f"agent-{slug}"
        assert_path_within(target, self._paths.workspace_root, description="Agent 目标目录")
        return target

    def _assert_target_available(self, target: Path) -> None:
        """拒绝现有、symlink 或大小写碰撞目录，生成器没有覆盖模式。"""
        if target_exists(target):
            raise FileExistsError(f"目标 Agent 目录已存在：{target}")
        if self._paths.agents_root.is_dir():
            target_name = target.name.casefold()
            collisions = [
                entry.name
                for entry in self._paths.agents_root.iterdir()
                if entry.name.casefold() == target_name
            ]
            if collisions:
                raise FileExistsError("目标 Agent 目录与现有路径大小写冲突：" + ", ".join(collisions))

    def _ensure_agents_root(self) -> None:
        """仅在所有输入已通过后创建普通 `agents/` 父目录。"""
        agents_root = self._paths.agents_root
        if agents_root.is_symlink():
            raise ValueError("agents 根目录不能是符号链接")
        if agents_root.exists() and not agents_root.is_dir():
            raise ValueError("agents 根路径必须是目录")
        agents_root.mkdir(parents=True, exist_ok=True)
        assert_path_within(agents_root, self._paths.workspace_root, description="agents 根目录")

    def _assert_existing_agent_directory(self, directory: Path) -> None:
        """在 validate/diff 前验证目录边界，避免读取开发者放入的 symlink。"""
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"Agent 目录不存在、不是目录或是符号链接：{directory}")
        assert_path_within(directory, self._paths.agents_root, description="Agent 目录")
        read_source_tree(directory)

    def _assert_config_path(self, path: Path) -> None:
        """配置文件必须位于普通 config 根内，不能用 symlink 或路径片段逃逸。"""
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"配置文件不存在、不是普通文件或是符号链接：{path}")
        assert_path_within(path, self._paths.config_root, description="Generator 配置")

    def _assert_template_tree(self, directory: Path) -> None:
        """模板源中的任意 symlink 都会让生成失败，而不是跟随未知内容。"""
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise ValueError(f"模板不能包含符号链接：{path}")

    def _load_descriptor(self, path: Path) -> AgentDescriptorV1:
        """从生成的 YAML 读取严格 descriptor。"""
        return load_yaml_model(path, AgentDescriptorV1)

    def _load_provenance(self, path: Path) -> SourceProvenanceV1:
        """从生成目录读取严格 provenance。"""
        return load_json_model(path, SourceProvenanceV1)

    def _assert_rendered_descriptor_matches_recipe(self, descriptor: AgentDescriptorV1, recipe: GenerationRecipeV1) -> None:
        """检查模板渲染后的 descriptor 仍完整反映已确认的身份与资源。"""
        spec = recipe.generation_spec
        profile = recipe.profile_input
        expected_tool_name = _tool_name_for_slug(spec.slug)
        mismatches = [
            field_name
            for field_name, actual, expected in (
                ("agent_id", descriptor.agent_id, spec.agent_id),
                ("slug", descriptor.slug, spec.slug),
                ("tool_name", descriptor.tool_name, expected_tool_name),
                ("version", descriptor.version, profile.agent_version),
                ("model_alias", descriptor.model_alias, spec.model_alias),
                ("resource_id", descriptor.resources[0].resource_id, spec.resource_id),
                ("skill_ref", descriptor.resources[0].skill_ref, spec.skill_revision),
                ("template_id", descriptor.source.template_id, spec.template_id),
                ("template_version", descriptor.source.template_version, spec.template_version),
            )
            if actual != expected
        ]
        if descriptor.display_name != profile.profile.display_name:
            mismatches.append("display_name")
        if descriptor.description != profile.profile.description:
            mismatches.append("description")
        if descriptor.supported_intents != profile.profile.supported_intents:
            mismatches.append("supported_intents")
        if mismatches:
            raise ValueError("模板渲染的 descriptor 与 generation recipe 不一致：" + ", ".join(mismatches))

    def _assert_descriptor_matches_provenance(
        self,
        descriptor: AgentDescriptorV1,
        provenance: SourceProvenanceV1,
    ) -> None:
        """防止 descriptor 被替换为不同模板而 provenance 仍伪装为原始来源。"""
        if descriptor.source.template_id != provenance.template_id:
            raise ValueError("agent.yaml template_id 与 provenance 不一致")
        if descriptor.source.template_version != provenance.template_version:
            raise ValueError("agent.yaml template_version 与 provenance 不一致")

    def _assert_stable_descriptor_fields(self, descriptor: AgentDescriptorV1, recipe: GenerationRecipeV1) -> None:
        """只冻结 Agent 身份和首次模板来源，允许开发者接管可演进 descriptor 字段。"""
        spec = recipe.generation_spec
        expected_tool_name = _tool_name_for_slug(spec.slug)
        mismatches = [
            field_name
            for field_name, actual, expected in (
                ("agent_id", descriptor.agent_id, spec.agent_id),
                ("slug", descriptor.slug, spec.slug),
                ("tool_name", descriptor.tool_name, expected_tool_name),
            )
            if actual != expected
        ]
        if mismatches:
            raise ValueError("agent.yaml 修改了不可变身份字段：" + ", ".join(mismatches))

    def _assert_provenance_matches_recipe(self, provenance: SourceProvenanceV1, recipe: GenerationRecipeV1) -> None:
        """逐项复核 provenance 的可重放来源，防止格式合法的审计字段被替换。"""
        spec = recipe.generation_spec
        knowledge = recipe.knowledge
        expected = (
            ("template_id", provenance.template_id, spec.template_id),
            ("template_version", provenance.template_version, spec.template_version),
            ("sdk_version", provenance.sdk_version, spec.sdk_version),
            ("generation_spec_checksum", provenance.generation_spec_checksum, spec.input_checksum),
            ("knowledge_resource_checksum", provenance.knowledge_resource_checksum, knowledge.resource_checksum),
            ("skill_checksum", provenance.skill_checksum, spec.skill_checksum),
            ("profile_checksum", provenance.profile_checksum, spec.agent_profile_checksum),
        )
        mismatches = [field_name for field_name, actual, expected_value in expected if actual != expected_value]
        if mismatches:
            raise ValueError("provenance 与 generation recipe 不一致：" + ", ".join(mismatches))

    def _assert_generation_approvals(
        self,
        *,
        profile_input: AgentProfileInputV1,
        knowledge: KnowledgeGenerationInputV1,
    ) -> None:
        """在写 staging 前要求 Resource、Skill、Profile 都有当前版本的人工确认。"""
        config_root = self._paths.config_root
        assert_approval(
            config_root,
            subject_type="resource",
            slug=knowledge.knowledge_slug,
            revision=knowledge.resource_revision,
            checksum=knowledge.resource_checksum,
        )
        assert_approval(
            config_root,
            subject_type="skill",
            slug=knowledge.knowledge_slug,
            revision=knowledge.skill_revision,
            checksum=knowledge.skill_checksum,
        )
        assert_approval(
            config_root,
            subject_type="profile",
            slug=profile_input.slug,
            revision=profile_input.profile_revision,
            checksum=canonical_checksum(profile_input.profile.model_dump(mode="json")),
        )

    @staticmethod
    def _expected_generated_files() -> set[str]:
        """返回当前 Generator 固定输出的完整基线集合，开发者新增文件不属于该集合。"""
        return set(_RENDERED_FILES.values()) | {RECIPE_FILE_NAME, PROVENANCE_FILE_NAME}

    def _compile_and_import_generated_agent(
        self,
        staging: Path,
        descriptor: AgentDescriptorV1,
        recipe: GenerationRecipeV1,
    ) -> None:
        """执行无网络的 compile/import smoke，不启动 HTTP 服务或调用 Agent 工具。"""
        agent_path = staging / "agent.py"
        main_path = staging / "main.py"
        for source_path in (agent_path, main_path):
            compile(source_path.read_text(encoding="utf-8"), str(source_path), "exec")
        module_name = f"muye_generated_{canonical_checksum(recipe.generation_spec.agent_id)[:16]}"
        specification = importlib.util.spec_from_file_location(module_name, agent_path)
        if specification is None or specification.loader is None:
            raise ValueError("无法为生成的 agent.py 创建 import specification")
        module = importlib.util.module_from_spec(specification)
        sys.modules[module_name] = module
        previous_dont_write_bytecode = sys.dont_write_bytecode
        sys.dont_write_bytecode = True
        try:
            specification.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = previous_dont_write_bytecode
            sys.modules.pop(module_name, None)
        class_name = _class_name_for_slug(descriptor.slug)
        if not isinstance(getattr(module, class_name, None), type):
            raise ValueError(f"生成的 agent.py 未导出预期 Agent 类：{class_name}")

    def _rename_without_overwrite(self, staging: Path, target: Path) -> None:
        """以 Linux renameat2 的 NOREPLACE 原子发布 staging，绝不替换已有目录。"""
        self._assert_target_available(target)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            renameat2 = libc.renameat2
        except AttributeError as exc:
            raise OSError("当前平台不支持原子 no-replace 目录发布") from exc
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            _AT_FDCWD,
            os.fsencode(staging),
            _AT_FDCWD,
            os.fsencode(target),
            _RENAME_NOREPLACE,
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        if error_number == errno.EEXIST:
            raise FileExistsError(f"目标 Agent 目录已存在：{target}")
        raise OSError(error_number, os.strerror(error_number), target)


def _validate_cli_slug(slug: str, *, label: str) -> None:
    """在触及路径前验证 CLI slug，阻止绝对路径、遍历和控制字符。"""
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) is None:
        raise ValueError(f"{label} 必须是小写字母、数字和单连字符组成的 slug")


def _class_name_for_slug(slug: str) -> str:
    """将安全 slug 映射为稳定 Python 类名，不直接拼接未校验输入。"""
    parts = [part.capitalize() for part in slug.split("-")]
    if parts[0][0].isdigit():
        parts.insert(0, "Agent")
    return "Generated" + "".join(parts) + "Agent"


def _tool_name_for_slug(slug: str) -> str:
    """派生符合 SDK 名称限制且在超长 slug 下仍稳定的工具名称。"""
    base = slug.replace("-", "_")
    if base[0].isdigit():
        base = f"agent_{base}"
    candidate = f"{base}_agent"
    if len(candidate) <= 63:
        return candidate
    suffix = canonical_checksum(slug)[:8]
    return f"{base[:54]}_{suffix}"


def _render_template(content: str, *, context: dict[str, str], source_path: Path) -> str:
    """以一轮字面量替换有限占位符，禁止 Jinja/Python 语义和遗漏变量。"""
    placeholders = set(_PLACEHOLDER_PATTERN.findall(content))
    unknown = placeholders - set(context)
    if unknown:
        raise ValueError(f"模板包含未知变量 {source_path}: {', '.join(sorted(unknown))}")
    rendered = _PLACEHOLDER_PATTERN.sub(lambda match: context[match.group(1)], content)
    if _PLACEHOLDER_PATTERN.search(rendered):
        raise ValueError(f"模板渲染后仍包含占位符：{source_path}")
    return rendered


def _stable_json(value: object) -> str:
    """产生可嵌入 YAML/Python 的 JSON 字面量，并统一终止换行。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def _json_literal(value: object) -> str:
    """生成可直接嵌入 YAML 或 Python 的单行 JSON 字面量。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ": "))


def _unified_tree_diff(actual_tree: dict[str, str], expected_tree: dict[str, str]) -> str:
    """按路径稳定排序生成完整 unified diff，且不写任何目标文件。"""
    lines: list[str] = []
    for path in sorted(set(actual_tree) | set(expected_tree)):
        actual = actual_tree.get(path, "")
        expected = expected_tree.get(path, "")
        if actual == expected and path in actual_tree and path in expected_tree:
            continue
        lines.extend(
            difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=f"current/{path}",
                tofile=f"template/{path}",
            )
        )
    return "".join(lines)


def _semver_sort_key(value: str) -> tuple[int, int, int, int, tuple[tuple[int, int | str], ...]]:
    """按 SemVer 2.0.0 选择最新模板，正确处理数字 prerelease 标识和 build metadata。"""
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if match is None:
        raise ValueError(f"无法排序非 SemVer 模板版本：{value}")
    prerelease = match.group(4)
    prerelease_identifiers = ()
    if prerelease:
        prerelease_identifiers = tuple(
            (0, int(identifier)) if identifier.isdigit() else (1, identifier)
            for identifier in prerelease.split(".")
        )
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        1 if prerelease is None else 0,
        prerelease_identifiers,
    )
