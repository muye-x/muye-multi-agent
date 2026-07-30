"""Knowledge Worker 的稳定错误分类。"""

from __future__ import annotations


class KnowledgePipelineError(ValueError):
    """带可审计错误码的受控 Worker 失败。"""

    code = "PIPELINE_FAILED"


class DependencyUnavailableError(KnowledgePipelineError):
    """所选解析器、Embedding 或 Milvus 客户端依赖没有安装。"""

    code = "DEPENDENCY_UNAVAILABLE"


class OcrRequiredError(KnowledgePipelineError):
    """扫描 PDF 未获得文字且未启用 OCR capability。"""

    code = "OCR_REQUIRED"


class ParserFailedError(KnowledgePipelineError):
    """文件格式、编码或解析预算不满足发布要求。"""

    code = "PARSER_FAILED"


class ApprovalRequiredError(KnowledgePipelineError):
    """Schema Proposal 未被精确 checksum 确认。"""

    code = "APPROVAL_REQUIRED"


class EvaluationGateError(KnowledgePipelineError):
    """评测指标未达到发布门禁。"""

    code = "EVALUATION_FAILED"


class JobCancelledError(KnowledgePipelineError):
    """作业收到协作式取消请求，后续写入步骤必须停止。"""

    code = "CANCELLED"
