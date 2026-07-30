"""Generator 输入审批记录的路径边界、读取和原子写入。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from .io import assert_path_within, load_json_model, write_json_atomic
from .models import GenerationApprovalV1


ApprovalSubjectType = Literal["resource", "skill", "profile"]
_APPROVAL_SUBJECT_TYPES = frozenset({"resource", "skill", "profile"})


def approval_path(config_root: Path, *, subject_type: ApprovalSubjectType, slug: str) -> Path:
    """返回一个受控审批文件路径，不允许调用方指定任意目录或文件名。"""
    if subject_type not in _APPROVAL_SUBJECT_TYPES:
        raise ValueError(f"不支持的 approval subject type：{subject_type}")
    path = config_root / "approvals" / subject_type / f"{slug}.json"
    assert_path_within(path, config_root, description="审批记录")
    return path


def write_approval(config_root: Path, approval: GenerationApprovalV1) -> Path:
    """将开发者确认写为可提交的 JSON 记录，覆盖仅限同一受控审批路径。"""
    path = approval_path(
        config_root,
        subject_type=approval.subject_type,
        slug=approval.subject_slug,
    )
    approval_root = config_root / "approvals"
    subject_directory = path.parent
    for directory in (approval_root, subject_directory):
        if directory.is_symlink():
            raise ValueError(f"审批目录不能是符号链接：{directory}")
        directory.mkdir(parents=True, exist_ok=True)
        if not directory.is_dir():
            raise ValueError(f"审批目录必须是目录：{directory}")
        assert_path_within(directory, config_root, description="审批目录")
    write_json_atomic(path, approval.model_dump(mode="json"))
    return path


def assert_approval(
    config_root: Path,
    *,
    subject_type: ApprovalSubjectType,
    slug: str,
    revision: str,
    checksum: str,
) -> GenerationApprovalV1:
    """确认当前输入存在精确匹配的审批记录，否则在生成前 fail closed。"""
    path = approval_path(config_root, subject_type=subject_type, slug=slug)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"缺少已确认的 {subject_type} 审批记录：{path}")
    approval = load_json_model(path, GenerationApprovalV1)
    expected = (subject_type, slug, revision, checksum)
    actual = (approval.subject_type, approval.subject_slug, approval.revision, approval.checksum)
    if actual != expected:
        raise ValueError(f"{subject_type} 审批记录与当前输入 revision/checksum 不一致")
    return approval
