"""可复用的本地子进程启动、健康检查与清理实现。"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import time
from typing import Mapping
from urllib.request import urlopen


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """一个由本地监督器管理的进程及其健康检查边界。"""

    name: str
    command: tuple[str, ...]
    cwd: Path
    health_url: str
    environment: Mapping[str, str] = field(default_factory=dict)


class LocalRuntimeSupervisor:
    """启动本次会话拥有的服务，复用健康的基础依赖并在退出时仅清理自有进程。"""

    def __init__(self, *, workspace_root: Path) -> None:
        self._workspace_root = workspace_root
        self._owned_processes: list[subprocess.Popen[str]] = []

    def start(self, spec: ServiceSpec, *, timeout_seconds: float = 45.0, reuse_healthy: bool = True) -> bool:
        """启动服务并等待健康；返回值表示服务是否由当前会话创建。"""

        already_healthy = self._healthy(spec.health_url)
        if already_healthy:
            if reuse_healthy:
                print(f"[dev] reusing healthy {spec.name}: {spec.health_url}", flush=True)
                return False
            raise RuntimeError(
                f"{spec.name} 已由其他进程监听：{spec.health_url}；"
                "请先停止该进程后重新启动 local-dev 会话"
            )
        environment = os.environ.copy()
        environment.update(spec.environment)
        existing_pythonpath = environment.get("PYTHONPATH", "")
        pythonpath = [str(self._workspace_root), str(spec.cwd)]
        if existing_pythonpath:
            pythonpath.append(existing_pythonpath)
        environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
        environment["PYTHONUNBUFFERED"] = "1"
        print(f"[dev] starting {spec.name}", flush=True)
        process = subprocess.Popen(
            spec.command,
            cwd=spec.cwd,
            env=environment,
            text=True,
            start_new_session=True,
        )
        self._owned_processes.append(process)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"{spec.name} 启动后异常退出，退出码：{process.returncode}")
            if self._healthy(spec.health_url):
                print(f"[dev] {spec.name} ready: {spec.health_url}", flush=True)
                return True
            time.sleep(0.5)
        raise RuntimeError(f"{spec.name} 健康检查超时：{spec.health_url}")

    def stop(self) -> None:
        """以反向依赖顺序终止本会话创建的进程。"""

        for process in reversed(self._owned_processes):
            if process.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                continue
        deadline = time.monotonic() + 5
        for process in self._owned_processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
        self._owned_processes.clear()

    @staticmethod
    def _healthy(url: str) -> bool:
        try:
            with urlopen(url, timeout=1.5) as response:  # noqa: S310 - URLs are internal specs.
                return response.status == 200
        except OSError:
            return False
