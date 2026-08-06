"""临时 candidate muye-data 进程的受控生命周期。"""

from __future__ import annotations

from contextlib import AbstractContextManager
import os
from pathlib import Path
import socket
import subprocess
import time
from typing import Any

import httpx
import yaml


class CandidateDataService(AbstractContextManager["CandidateDataService"]):
    """仅为评测启动 loopback `muye-data`，退出时必定终止子进程。

    该进程加载 candidate Snapshot 且关闭阶段 5 Agent 身份校验；它不会暴露公网端口，
    也不会读取或打印任何凭据。Milvus token 仍只通过已存在的环境变量解析。
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        slug: str,
        connection: str,
        llm_base_url: str = "http://127.0.0.1:9850",
        milvus_uri: str = "http://127.0.0.1:19530",
        milvus_token: str | None = None,
    ) -> None:
        self._workspace_root = workspace_root
        self._slug = slug
        self._connection = connection
        self._llm_base_url = llm_base_url.rstrip("/")
        self._milvus_uri = milvus_uri.rstrip("/")
        self._milvus_token = milvus_token.strip() if milvus_token else None
        self._process: subprocess.Popen[str] | None = None
        self.base_url = ""

    def __enter__(self) -> "CandidateDataService":
        port = _available_loopback_port()
        config_path = self._write_config()
        log_path = self._workspace_root / "config" / "generated" / "agent-creation-candidates" / f"{self._slug}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH", "")
        pythonpath_entries = [str(self._workspace_root), str(self._workspace_root / "muye-data")]
        if existing_pythonpath:
            pythonpath_entries.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        environment.update(
            {
                "MUYE_DATA_HOST": "127.0.0.1",
                "MUYE_DATA_PORT": str(port),
                "MUYE_DATA_WORKERS": "1",
                "MUYE_DATA_CONFIG_PATH": str(config_path),
                "MUYE_DATA_RESOURCE_SNAPSHOT_PATH": str(
                    self._workspace_root / "config" / "generated" / "resource-snapshot.candidate.json"
                ),
                "MUYE_DATA_AGENT_AUTH_ENABLED": "false",
                "MUYE_DATA_LLM_BASE_URL": self._llm_base_url,
            }
        )
        if self._milvus_token:
            environment["MUYE_KNOWLEDGE_MILVUS_TOKEN"] = self._milvus_token
        try:
            with log_path.open("w", encoding="utf-8") as stream:
                self._process = subprocess.Popen(
                    [str(self._workspace_root / ".venv" / "bin" / "python"), "main.py"],
                    cwd=self._workspace_root / "muye-data",
                    env=environment,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            self.base_url = f"http://127.0.0.1:{port}"
            self._wait_ready()
            return self
        except Exception:
            self._stop()
            raise

    def __exit__(self, *_: object) -> None:
        self._stop()

    def _stop(self) -> None:
        """终止本实例启动的进程；启动失败时也必须执行这一清理。"""
        process = self._process
        if process is None:
            return
        if process.poll() is not None:
            self._process = None
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        finally:
            self._process = None

    def _write_config(self) -> Path:
        connection: dict[str, Any] = {"type": "milvus", "uri": self._milvus_uri}
        if self._milvus_token:
            connection["token_env"] = "MUYE_KNOWLEDGE_MILVUS_TOKEN"
        path = self._workspace_root / "config" / "generated" / "agent-creation-candidates" / f"{self._slug}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump({"version": 1, "connections": {self._connection: connection}, "resources": {}}, sort_keys=False), encoding="utf-8")
        return path

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                raise RuntimeError("临时 candidate muye-data 启动失败")
            try:
                response = httpx.get(f"{self.base_url}/ready", timeout=1.0, trust_env=False)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("临时 candidate muye-data 未在 30 秒内就绪")


def _available_loopback_port() -> int:
    """为临时评测选择一个 loopback 临时端口。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])
