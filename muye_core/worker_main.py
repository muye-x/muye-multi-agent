"""阶段 2 Knowledge Worker 独立进程入口。"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
from threading import Event

from .knowledge_backend import create_configured_knowledge_backend
from .proposals import LLMProfileProposalBackend
from .postgres import PostgresCoreStore
from .storage import ArtifactStore
from .worker import KnowledgeJobWorker, ProfileProposalJobWorker


logger = logging.getLogger(__name__)


def main() -> None:
    """持续领取 BUILD Job；进程终止不会伪造当前 Job 的完成状态。"""

    database_url = _required("MUYE_CORE_DATABASE_URL")
    artifact_store = ArtifactStore(Path(os.environ.get("MUYE_CORE_ARTIFACT_ROOT", "/var/lib/muye/artifacts")))
    store = PostgresCoreStore(database_url)
    worker_id = os.environ.get("MUYE_CORE_KNOWLEDGE_WORKER_ID", f"knowledge-worker-{os.getpid()}").strip()
    poll_seconds = float(os.environ.get("MUYE_CORE_WORKER_POLL_SECONDS", "1"))
    lease_seconds = int(os.environ.get("MUYE_CORE_WORKER_LEASE_SECONDS", "60"))
    if not worker_id or poll_seconds <= 0:
        raise ValueError("Knowledge Worker identity 或轮询间隔无效")
    worker = KnowledgeJobWorker(
        store=store,
        backend=create_configured_knowledge_backend(store=store, artifact_store=artifact_store),
        artifact_store=artifact_store,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    proposal_worker = ProfileProposalJobWorker(
        store=store,
        backend=LLMProfileProposalBackend(
            artifact_store=artifact_store,
            llm_base_url=_required("MUYE_CORE_LLM_BASE_URL"),
            embedding_alias=os.environ.get("MUYE_CORE_EMBEDDING_ALIAS", "embedding_default"),
            evaluation_case_count=int(os.environ.get("MUYE_CORE_PROPOSAL_CASE_COUNT", "12")),
            ocr_available=os.environ.get("MUYE_CORE_OCR_AVAILABLE", "false").lower() == "true",
        ),
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    stopped = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    logger.info("knowledge worker started worker_id=%s", worker_id)
    while not stopped.is_set():
        if proposal_worker.run_once() is None and worker.run_once() is None:
            stopped.wait(poll_seconds)
    logger.info("knowledge worker stopped worker_id=%s", worker_id)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Knowledge Worker 需要 {name}")
    return value


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    main()
