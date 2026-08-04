"""Compose 环境的只读诊断与最小 smoke 命令。"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
from typing import Sequence

import httpx
from dotenv import dotenv_values


_COMPOSE_ENV_FILES = (
    "control_server/.env",
    "muye-llm/.env",
    "muye-data/.env",
    "agents/agent-main/.env",
    "muye-gateway/.env",
)


def _read_module_environment(root: Path, relative_path: str) -> dict[str, str]:
    """读取一个模块的 `.env`，允许 Shell 对同名字段显式覆盖。"""

    path = root / relative_path
    values = (
        {key: value for key, value in dotenv_values(path).items() if value is not None}
        if path.is_file()
        else {}
    )
    return {**values, **os.environ}


def _compose(root: Path, arguments: Sequence[str]) -> int:
    """用固定 compose 文件运行只读子命令，避免 shell 注入。"""
    command = ["docker", "compose", "--project-name", os.environ.get("MUYE_COMPOSE_PROJECT_NAME", "muye")]
    for env_file in _COMPOSE_ENV_FILES:
        command.extend(["--env-file", env_file])
    command.extend(["-f", "compose.yaml", "-f", "compose.agents.generated.yaml", *arguments])
    return subprocess.run(command, cwd=root, check=False).returncode


def doctor(root: Path) -> int:
    """验证必要配置、Compose 语法与运行服务健康状态。"""
    control = _read_module_environment(root, "control_server/.env")
    agent_main = _read_module_environment(root, "agents/agent-main/.env")
    gateway = _read_module_environment(root, "muye-gateway/.env")
    required = (
        ("control_server/.env", control, "MUYE_CONTROL_DATABASE_URL"),
        ("control_server/.env", control, "MUYE_CONTROL_OPERATOR_TOKEN"),
        ("control_server/.env", control, "MUYE_CONTROL_MAIN_TOKEN"),
        ("control_server/.env", control, "MUYE_CONTROL_HEALTH_TOKEN"),
        ("control_server/.env", control, "MUYE_CONTROL_GATEWAY_TOKEN"),
        ("agents/agent-main/.env", agent_main, "MUYE_CONTROL_MAIN_TOKEN"),
        ("agents/agent-main/.env", agent_main, "MUYE_MAIN_CALLER_TOKEN"),
        ("muye-gateway/.env", gateway, "MUYE_GATEWAY_CONTROL_TOKEN"),
        ("muye-gateway/.env", gateway, "MUYE_MAIN_CALLER_TOKEN"),
    )
    missing = [f"{path}:{name}" for path, values, name in required if not values.get(name, "").strip()]
    if missing:
        print("error: missing required configuration: " + ", ".join(missing))
        return 2
    shared_values = (
        ("MUYE_CONTROL_MAIN_TOKEN", control["MUYE_CONTROL_MAIN_TOKEN"], "agents/agent-main/.env:MUYE_CONTROL_MAIN_TOKEN", agent_main["MUYE_CONTROL_MAIN_TOKEN"]),
        ("MUYE_CONTROL_GATEWAY_TOKEN", control["MUYE_CONTROL_GATEWAY_TOKEN"], "muye-gateway/.env:MUYE_GATEWAY_CONTROL_TOKEN", gateway["MUYE_GATEWAY_CONTROL_TOKEN"]),
        ("MUYE_MAIN_CALLER_TOKEN", agent_main["MUYE_MAIN_CALLER_TOKEN"], "muye-gateway/.env:MUYE_MAIN_CALLER_TOKEN", gateway["MUYE_MAIN_CALLER_TOKEN"]),
    )
    for left_name, left_value, right_name, right_value in shared_values:
        if left_value != right_value:
            print(f"error: {left_name} must match {right_name}")
            return 2
    logical_tokens = (
        control["MUYE_CONTROL_OPERATOR_TOKEN"],
        control["MUYE_CONTROL_MAIN_TOKEN"],
        control["MUYE_CONTROL_HEALTH_TOKEN"],
        control["MUYE_CONTROL_GATEWAY_TOKEN"],
        agent_main["MUYE_MAIN_CALLER_TOKEN"],
    )
    if len(set(logical_tokens)) != len(logical_tokens):
        print("error: control operator, main, health, gateway, and caller tokens must be distinct")
        return 2
    return _compose(root, ("config", "--quiet"))


def smoke(root: Path) -> int:
    """验证 Gateway 与 Control health；不打印认证凭据。"""
    base_url = _read_module_environment(root, "muye-gateway/.env").get("MUYE_GATEWAY_BASE_URL", "").rstrip("/")
    if not base_url:
        print("error: MUYE_GATEWAY_BASE_URL is required")
        return 2
    try:
        response = httpx.get(f"{base_url}/gateway/health", timeout=10.0, trust_env=False)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"error: gateway smoke failed: {type(exc).__name__}")
        return 1
    print("smoke: gateway healthy")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """解析运维命令，并以当前 Scaffold 根目录作为唯一操作范围。"""
    parser = argparse.ArgumentParser(prog="muye.sh")
    parser.add_argument("command", choices=("doctor", "smoke"))
    arguments = parser.parse_args(argv)
    root = Path.cwd().resolve(strict=True)
    return doctor(root) if arguments.command == "doctor" else smoke(root)


if __name__ == "__main__":
    raise SystemExit(main())
