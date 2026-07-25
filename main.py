#!/usr/bin/env python
"""
Muye Multi-Agent Scaffold 服务启动器。

按依赖顺序启动 muye-llm、agent-main、agent-travel、agent-order 与本地 Gateway 控制台。

外部依赖（需提前启动）：
  - PostgreSQL 16  — checkpointer 持久化存储
  - Redis（可选）  — 短期记忆（由 MEMORY_ENABLE_REDIS 控制）

用法:
    python main.py
    python main.py --timeout 120
    python main.py --dry-run

停止:
    Ctrl+C —— 优雅关闭子进程
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from dotenv import dotenv_values, load_dotenv

# ─── 颜色输出 ────────────────────────────────────────────────────────────────

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"


def _c(color: str, text: str) -> str:
    """包裹 ANSI 颜色码。"""
    return f"{color}{text}{RESET}"


PROJECT_ROOT = Path(__file__).resolve().parent
ROOT_ENV_FILE = PROJECT_ROOT / ".env"
LLM_ENV_FILE = PROJECT_ROOT / "muye-llm" / ".env"
AGENT_MAIN_ENV_FILE = PROJECT_ROOT / "agents" / "agent-main" / ".env"
ENV_EXAMPLE_FILE = PROJECT_ROOT / ".env.example"
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
DEFAULT_PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
PYTHON_BIN = os.environ.get("MUYE_PYTHON_BIN", str(DEFAULT_PYTHON))

SERVICES: list[dict] = [
    {
        "id": 1,
        "name": "muye-llm · 模型网关",
        "cwd": "muye-llm",
        "cmd": [PYTHON_BIN, "main.py"],
        "port": 9850,
        "health_url": "http://127.0.0.1:9850/health",
        "bind_host": "127.0.0.1",
        "log_label": "LLM",
        "log_color": "\033[96m",
    },
    {
        "id": 2,
        "name": "agent-main · 主 Agent",
        "cwd": "agents/agent-main",
        "cmd": [PYTHON_BIN, "main.py"],
        "port": 9860,
        "health_url": "http://127.0.0.1:9860/health",
        "bind_host": "127.0.0.1",
        "log_label": "MAIN",
        "log_color": "\033[93m",
    },
    {
        "id": 3,
        "name": "agent-travel · 旅行参考服务",
        "cwd": "agents/agent-travel",
        "cmd": [PYTHON_BIN, "main.py"],
        "port": 8011,
        "health_url": "http://127.0.0.1:8011/health",
        "bind_host": "127.0.0.1",
        "log_label": "TRAVEL",
        "log_color": "\033[92m",
    },
    {
        "id": 4,
        "name": "agent-order · Graph 参考服务",
        "cwd": "agents/agent-order",
        "cmd": [PYTHON_BIN, "main.py"],
        "port": 8012,
        "health_url": "http://127.0.0.1:8012/health",
        "bind_host": "127.0.0.1",
        "log_label": "ORDER",
        "log_color": "\033[95m",
    },
    {
        "id": 5,
        "name": "muye-gateway · 运维控制台",
        "cwd": "muye-gateway",
        "cmd": [PYTHON_BIN, "dashboard_main.py"],
        "port": 9870,
        "health_url": "http://127.0.0.1:9870/health",
        "bind_host": "127.0.0.1",
        "log_label": "GATEWAY",
        "log_color": "\033[94m",
    },
]

# ─── 全局进程跟踪 ──────────────────────────────────────────────────────────────
_started_procs: list[subprocess.Popen] = []


def load_runtime_environment(env_file: Path = ROOT_ENV_FILE) -> None:
    """加载一键启动配置，已存在的进程环境变量保持最高优先级。"""
    if env_file.is_file():
        load_dotenv(env_file, override=False)


def read_llm_environment(
    llm_env_file: Path = LLM_ENV_FILE,
    agent_main_env_file: Path = AGENT_MAIN_ENV_FILE,
) -> dict[str, str]:
    """合并 LLM、主 Agent 本地配置与进程环境，供启动前检查使用。"""
    values: dict[str, str] = {}
    for env_file in (llm_env_file, agent_main_env_file):
        if not env_file.is_file():
            continue
        values.update(
            {
                name: value
                for name, value in dotenv_values(env_file).items()
                if value is not None
            }
        )
    values.update(os.environ)
    return values


def _is_http_url(value: str) -> bool:
    """判断配置值是否为包含主机名的 HTTP(S) URL。"""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def validate_llm_environment(values: Mapping[str, str]) -> list[str]:
    """校验启动 LLM 服务所需的配置，返回可直接展示的问题列表。"""
    errors: list[str] = []
    required_secrets = {
        "MUYE_LLM_API_KEY": "模型服务 API Key",
        "MUYE_LLM_EMBED_API_KEY": "Embedding 服务 API Key",
    }
    for name, description in required_secrets.items():
        if not values.get(name, "").strip():
            errors.append(f"{name} 未配置（{description}）")

    base_urls = {
        "MUYE_LLM_API_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "MUYE_LLM_EMBED_API_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    for name, default in base_urls.items():
        value = values.get(name, default).strip()
        if not _is_http_url(value):
            errors.append(f"{name} 必须是有效的 HTTP(S) URL")
        elif urlsplit(value).hostname == "your-openai-compatible-endpoint.example":
            errors.append(f"{name} 仍为 .env.example 中的占位地址")

    model_ids: list[str] | None = None
    raw_models = values.get("MUYE_LLM_MODELS_JSON")
    if raw_models is not None:
        try:
            models = json.loads(raw_models)
        except json.JSONDecodeError:
            errors.append("MUYE_LLM_MODELS_JSON 必须是合法的 JSON array")
        else:
            if not isinstance(models, list) or not models:
                errors.append("MUYE_LLM_MODELS_JSON 必须是非空 JSON array")
            elif not all(
                isinstance(model, dict)
                and isinstance(model.get("id"), str)
                and bool(model["id"].strip())
                for model in models
            ):
                errors.append("MUYE_LLM_MODELS_JSON 中每个模型都必须包含非空 id")
            else:
                model_ids = [model["id"].strip() for model in models]
                if len(model_ids) != len(set(model_ids)):
                    errors.append("MUYE_LLM_MODELS_JSON 中的模型 id 不能重复")

    default_model = values.get("MUYE_LLM_DEFAULT_MODEL", "deepseek-v4-flash").strip()
    if not default_model:
        errors.append("MUYE_LLM_DEFAULT_MODEL 不能为空")
    elif default_model == "your-model-alias":
        errors.append("MUYE_LLM_DEFAULT_MODEL 仍为 .env.example 中的占位模型")
    elif model_ids is not None and default_model not in model_ids:
        errors.append("MUYE_LLM_DEFAULT_MODEL 必须存在于 MUYE_LLM_MODELS_JSON")

    agent_model = values.get("MUYE_LLM_MODEL", "deepseek-v4-flash").strip()
    if not agent_model:
        errors.append("MUYE_LLM_MODEL 不能为空")
    elif agent_model == "your-model-alias":
        errors.append("MUYE_LLM_MODEL 仍为 .env.example 中的占位模型")
    elif model_ids is not None and agent_model not in model_ids:
        errors.append("MUYE_LLM_MODEL 必须存在于 MUYE_LLM_MODELS_JSON")

    return errors


def print_configuration_errors(errors: Sequence[str]) -> None:
    """输出可操作的启动配置提示，不启动任何子进程。"""
    print(_c(RED + BOLD, "\n启动前配置检查未通过，尚未启动任何服务："))
    for error in errors:
        print(f"  - {error}")
    print(f"\n请复制 {_c(BLUE, str(ENV_EXAMPLE_FILE))} 为 {_c(BLUE, str(ROOT_ENV_FILE))}，")
    print("填写实际运行值后重新启动。真实密钥不得提交到版本控制。", flush=True)


class _AlreadyRunningProc:
    pid = -1
    def poll(self):
        return None


def _terminate_proc(proc: subprocess.Popen) -> None:
    if isinstance(proc, _AlreadyRunningProc):
        return
    if proc.poll() is not None:
        return
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass


# ─── 子进程日志中继 ──────────────────────────────────────────────────────────
def _log_relay(pipe, label: str, color: str) -> None:
    """读取子进程 stdout/stderr 管道，带彩色标签前缀打印到父进程控制台。"""
    prefix = f"{color}[{label}]{RESET} "
    try:
        for line in pipe:
            sys.stdout.write(prefix + line)
            sys.stdout.flush()
    except Exception:
        pass


# ─── 健康检查 ─────────────────────────────────────────────────────────────────
def wait_for_healthy(name: str, health_url: str, timeout: int = 0) -> bool:
    start_ts = time.time()
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(health_url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        elapsed = int(time.time() - start_ts)
        if timeout > 0 and elapsed >= timeout:
            return False
        if attempt % 3 == 1:
            if timeout > 0:
                print(f"  {_c(YELLOW, '⏳')} 等待 {name} 就绪... (已等待 {elapsed}s / 超时 {timeout}s)", flush=True)
            else:
                print(f"  {_c(YELLOW, '⏳')} 等待 {name} 就绪... (已等待 {elapsed}s)", flush=True)
        time.sleep(3)


def _is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ─── 服务启动 ─────────────────────────────────────────────────────────────────
def start_service(svc: dict, health_timeout: int) -> Optional[subprocess.Popen]:
    name: str = svc["name"]
    cwd: Path = PROJECT_ROOT / svc["cwd"]
    cmd: list[str] = svc["cmd"]
    health_url: str = svc["health_url"]
    port: int = svc["port"]

    print(f"\n{_c(BOLD + CYAN, '▶')} 启动 {_c(BOLD, name)}")
    print(f"  工作目录: {_c(BLUE, str(cwd))}")
    print(f"  命令:     {_c(BLUE, ' '.join(cmd))}")

    if not cwd.exists():
        print(_c(RED, f"  ✘ 目录不存在: {cwd}"), flush=True)
        return None

    if _is_port_in_use(port):
        print(_c(YELLOW, f"  ⚠ 端口 {port} 已被占用，跳过启动"), flush=True)
        sentinel = _AlreadyRunningProc()
        _started_procs.append(sentinel)
        print(_c(GREEN, f"  ✔ {name} 已就绪"), flush=True)
        return sentinel

    env = os.environ.copy()
    pythonpath_entries = [str(cwd)]
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    if existing_pythonpath:
        pythonpath_entries.append(existing_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
    # 强制 Python 非缓冲输出，确保日志实时出现在控制台
    env["PYTHONUNBUFFERED"] = "1"

    # 服务配置统一读取 MUYE_AGENT_HOST 或 MUYE_LLM_HOST。
    bind_host: str = svc.get("bind_host", "127.0.0.1")
    if svc["id"] == 1:
        env["MUYE_LLM_HOST"] = bind_host
    elif svc["id"] in {2, 3, 4}:
        env["MUYE_AGENT_HOST"] = bind_host

    log_label: str = svc.get("log_label", svc["name"][:6])
    log_color: str = svc.get("log_color", CYAN)

    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), env=env, start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except FileNotFoundError as e:
        print(_c(RED, f"  ✘ 启动失败: {e}"), flush=True)
        return None

    # 启动日志中继线程，将子进程输出实时转发到父进程控制台
    relay = threading.Thread(
        target=_log_relay, args=(proc.stdout, log_label, log_color), daemon=True
    )
    relay.start()

    _started_procs.append(proc)
    ready = wait_for_healthy(name, health_url, timeout=health_timeout)

    if not ready:
        print(_c(RED, f"  ✘ {name} 健康检查超时"), flush=True)
        return None

    if proc.poll() is not None:
        print(_c(RED, f"  ✘ {name} 意外退出"), flush=True)
        return None

    print(_c(GREEN, f"  ✔ {name} 已就绪"), flush=True)
    return proc


# ─── dry-run 模式 ────────────────────────────────────────────────────────────
def dry_run(configuration_errors: Sequence[str] = ()) -> None:
    """检查服务入口与运行配置，但不启动子进程。"""
    print(f"\n{_c(BOLD + YELLOW, '⚡ DRY-RUN 模式')}\n")
    python_bin = Path(PYTHON_BIN)
    print(f"  Python (.venv): {_c(BLUE, str(python_bin))} {'✔' if python_bin.exists() else '✘'}\n")
    all_ok = python_bin.exists()
    for svc in SERVICES:
        cwd = PROJECT_ROOT / svc["cwd"]
        cmd_file = cwd / svc["cmd"][1]
        ok_cwd = cwd.exists()
        ok_cmd = cmd_file.exists()
        ok_py = Path(svc["cmd"][0]).exists()
        status = _c(GREEN, "✔") if (ok_cwd and ok_cmd and ok_py) else _c(RED, "✘")
        print(f"  {status} {svc['name']}")
        if not ok_cwd:
            print(_c(RED, f"      目录不存在: {cwd}"))
            all_ok = False
        if not ok_cmd:
            print(_c(RED, f"      入口文件不存在: {cmd_file}"))
            all_ok = False
        if not ok_py:
            print(_c(RED, f"      Python 不存在: {svc['cmd'][0]}"))
            all_ok = False
    print()
    if all_ok:
        print(_c(GREEN + BOLD, "服务入口检查通过"))
    else:
        print(_c(RED + BOLD, "服务入口检查存在错误"))
    if configuration_errors:
        print(_c(YELLOW + BOLD, "\n运行配置尚未完成："))
        for error in configuration_errors:
            print(f"  - {error}")
        print(f"  配置模板: {ENV_EXAMPLE_FILE}")


# ─── 关闭 ─────────────────────────────────────────────────────────────────────
def _graceful_shutdown(sig, frame):
    print(f"\n\n{_c(BOLD + YELLOW, '⚡ 正在关闭服务...')}", flush=True)
    for proc in reversed(_started_procs):
        _terminate_proc(proc)
    print(_c(BOLD + GREEN, "✅ 服务已停止"), flush=True)
    sys.exit(0)


# ─── 启动完成提示 ───────────────────────────────────────────────────────────
def _print_ready_guide() -> None:
    guide = f"""
{_c(BOLD + GREEN, "═" * 68)}
{_c(BOLD + GREEN, "  ✅ Muye 服务已就绪！")}

{_c(BOLD, "服务端口：")}
  9850 — muye-llm
  9860 — Main Agent
  8011 — Travel Agent
  8012 — Order Agent
  9870 — Gateway 运维控制台（本地：http://127.0.0.1:9870/console/）

{_c(BOLD, "调用入口：")}
    curl -N -X POST http://127.0.0.1:9860/api/v1/chat/stream \\
    -H 'Content-Type: application/json' \\
    -d '{{"user_input":"你好","user_id":"u1","session_id":"s1"}}'

{_c(BOLD + GREEN, "═" * 68)}
"""
    print(guide, flush=True)


# ─── 主函数 ─────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Muye Multi-Agent 一键启动")
    parser.add_argument("--timeout", type=int, default=0, help="健康检查超时")
    parser.add_argument("--dry-run", action="store_true", help="仅检查不启动")
    args = parser.parse_args()

    load_runtime_environment()
    configuration_errors = validate_llm_environment(read_llm_environment())

    if args.dry_run:
        dry_run(configuration_errors)
        return

    if configuration_errors:
        print_configuration_errors(configuration_errors)
        return

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    print(f"\n{_c(BOLD + GREEN, '═' * 68)}")
    print(f"{_c(BOLD + GREEN, '  Muye Multi-Agent 服务组')}")
    print(f"{_c(BOLD + GREEN, '═' * 68)}\n")

    procs = []
    for svc in SERVICES:
        res = start_service(svc, args.timeout)
        if res is None:
            print(_c(RED, "\n✘ 启动失败"))
            _graceful_shutdown(None, None)
        procs.append(res)

    _print_ready_guide()

    while True:
        for svc, p in zip(SERVICES, procs):
            if isinstance(p, _AlreadyRunningProc): continue
            if p.poll() is not None:
                print(_c(RED, f"\n⚠ {svc['name']} 已退出"))
        time.sleep(5)


if __name__ == "__main__":
    main()
