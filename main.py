#!/usr/bin/env python
"""
Muye Multi-Agent Scaffold 服务启动器。

按依赖顺序启动 muye-llm、可选 muye-data、各 Agent 与本地 Gateway 控制台。

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
from dotenv import dotenv_values

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
VENV_PYTHON = PROJECT_ROOT / ".venv" / "bin" / "python"
DEFAULT_PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
PYTHON_BIN = os.environ.get("MUYE_PYTHON_BIN", str(DEFAULT_PYTHON))

SERVICES: list[dict] = [
    {
        "id": 1,
        "name": "muye-llm · 模型网关",
        "cwd": "muye-llm",
        "cmd": [PYTHON_BIN, "main.py"],
        "env_file": "muye-llm/.env",
        "host_env": "MUYE_LLM_HOST",
        "port_env": "MUYE_LLM_PORT",
        "default_port": 9850,
        "log_label": "LLM",
        "log_color": "\033[96m",
    },
    {
        "id": 6,
        "name": "muye-data · 只读召回服务",
        "cwd": "muye-data",
        "cmd": [PYTHON_BIN, "main.py"],
        "host_env": "MUYE_DATA_HOST",
        "port_env": "MUYE_DATA_PORT",
        "default_port": 9840,
        "log_label": "DATA",
        "log_color": "\033[36m",
        "enabled_env": "MUYE_DATA_ENABLED",
        "env_file": "muye-data/.env",
    },
    {
        "id": 2,
        "name": "agent-main · 主 Agent",
        "cwd": "agents/agent-main",
        "cmd": [PYTHON_BIN, "main.py"],
        "env_file": "agents/agent-main/.env",
        "host_env": "MUYE_AGENT_HOST",
        "port_env": "MUYE_AGENT_PORT",
        "default_port": 9860,
        "log_label": "MAIN",
        "log_color": "\033[93m",
    },
    {
        "id": 5,
        "name": "muye-gateway · 运维控制台",
        "cwd": "muye-gateway",
        "cmd": [PYTHON_BIN, "dashboard_main.py"],
        "env_file": "muye-gateway/.env",
        "host_env": "MUYE_DASHBOARD_HOST",
        "port_env": "MUYE_DASHBOARD_PORT",
        "default_port": 9870,
        "log_label": "GATEWAY",
        "log_color": "\033[94m",
    },
]

# ─── 全局进程跟踪 ──────────────────────────────────────────────────────────────
_started_procs: list[subprocess.Popen] = []


def _env_flag_enabled(values: Mapping[str, str], name: str, *, default: bool = False) -> bool:
    """判断可选模块的本地启用开关。"""
    raw_value = values.get(name)
    if raw_value is None:
        return default
    return raw_value.strip().lower() in {"1", "true", "yes", "on"}


def _service_environment(service: Mapping[str, object]) -> dict[str, str]:
    """只读取一个服务自身的 `.env`，不将模块配置合并到全局环境。"""

    relative_path = service.get("env_file")
    if not isinstance(relative_path, str):
        return {}
    path = PROJECT_ROOT / relative_path
    if not path.is_file():
        return {}
    return {name: value for name, value in dotenv_values(path).items() if value is not None}


def _service_runtime_environment(service: Mapping[str, object]) -> dict[str, str]:
    """按 Shell 优先级读取单个服务的运行配置。"""

    return {**_service_environment(service), **os.environ}


def service_runtime_address(service: Mapping[str, object]) -> tuple[str, int, str]:
    """返回服务监听地址和本地健康检查 URL，并校验模块端口配置。"""

    host_env = str(service["host_env"])
    port_env = str(service["port_env"])
    values = _service_runtime_environment(service)
    host = values.get(host_env, "127.0.0.1").strip()
    if not host:
        raise ValueError(f"{host_env} 不能为空")
    raw_port = values.get(port_env, str(service["default_port"])).strip()
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError(f"{port_env} 必须是 1 至 65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"{port_env} 必须是 1 至 65535 的整数")
    health_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    formatted_host = f"[{health_host}]" if ":" in health_host else health_host
    return host, port, f"http://{formatted_host}:{port}/health"


def enabled_services(values: Mapping[str, str] | None = None) -> list[dict]:
    """返回已启用服务；shell 环境仅可显式覆盖目标模块的开关。"""
    return [
        service
        for service in SERVICES
        if "enabled_env" not in service
        or _env_flag_enabled(
            values if values is not None else _service_runtime_environment(service),
            str(service["enabled_env"]),
        )
    ]


def _is_http_url(value: str) -> bool:
    """供兼容的独立校验调用判断 HTTP(S) URL。"""

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

    raw_embed_models = values.get("MUYE_LLM_EMBED_MODELS_JSON")
    if raw_embed_models is not None:
        try:
            embed_models = json.loads(raw_embed_models)
        except json.JSONDecodeError:
            errors.append("MUYE_LLM_EMBED_MODELS_JSON 必须是合法的 JSON array")
        else:
            embed_ids = [
                item.get("id", "").strip()
                for item in embed_models
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and isinstance(item.get("dimensions"), int)
                and item["dimensions"] > 0
            ] if isinstance(embed_models, list) else []
            if not embed_ids or len(embed_ids) != len(embed_models):
                errors.append("MUYE_LLM_EMBED_MODELS_JSON 中每个模型都必须包含非空 id 和正整数 dimensions")
            elif len(embed_ids) != len(set(embed_ids)):
                errors.append("MUYE_LLM_EMBED_MODELS_JSON 中的模型 id 不能重复")
            else:
                default_embed = values.get("MUYE_LLM_EMBED_DEFAULT_MODEL", "").strip()
                if default_embed not in embed_ids:
                    errors.append("MUYE_LLM_EMBED_DEFAULT_MODEL 必须存在于 MUYE_LLM_EMBED_MODELS_JSON")
    elif not values.get("MUYE_LLM_EMBED_MODEL", "text-embedding-v3").strip():
        errors.append("MUYE_LLM_EMBED_MODEL 不能为空")

    rerank_flag = values.get("MUYE_LLM_RERANK_ENABLED", "false").strip().lower()
    if rerank_flag not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        errors.append("MUYE_LLM_RERANK_ENABLED 必须是布尔值")
    elif rerank_flag in {"1", "true", "yes", "on"}:
        rerank_url = values.get("MUYE_LLM_RERANK_API_URL", "").strip()
        if not _is_http_url(rerank_url):
            errors.append("MUYE_LLM_RERANK_API_URL 必须是有效的完整 HTTP(S) URL")
        if not values.get("MUYE_LLM_RERANK_API_KEY", "").strip():
            errors.append("MUYE_LLM_RERANK_API_KEY 未配置（Rerank 服务 API Key）")
        try:
            rerank_models = json.loads(values.get("MUYE_LLM_RERANK_MODELS_JSON", "[]"))
        except json.JSONDecodeError:
            errors.append("MUYE_LLM_RERANK_MODELS_JSON 必须是合法的 JSON array")
        else:
            rerank_ids = [
                item.get("id", "").strip()
                for item in rerank_models
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            ] if isinstance(rerank_models, list) else []
            if not rerank_ids or len(rerank_ids) != len(rerank_models):
                errors.append("MUYE_LLM_RERANK_MODELS_JSON 中每个模型都必须包含非空 id")
            elif len(rerank_ids) != len(set(rerank_ids)):
                errors.append("MUYE_LLM_RERANK_MODELS_JSON 中的模型 id 不能重复")
            else:
                default_rerank = values.get("MUYE_LLM_RERANK_DEFAULT_MODEL", "").strip()
                if default_rerank not in rerank_ids:
                    errors.append("MUYE_LLM_RERANK_DEFAULT_MODEL 必须存在于 MUYE_LLM_RERANK_MODELS_JSON")

    return errors


def validate_data_environment(
    values: Mapping[str, str],
    project_root: Path = PROJECT_ROOT,
) -> list[str]:
    """仅在启用 muye-data 时校验本地入口配置，不探测远端数据库。"""
    raw_enabled = values.get("MUYE_DATA_ENABLED", "false").strip().lower()
    valid_flags = {"0", "1", "false", "true", "no", "yes", "off", "on"}
    if raw_enabled not in valid_flags:
        return ["MUYE_DATA_ENABLED 必须是布尔值"]
    if raw_enabled not in {"1", "true", "yes", "on"}:
        return []

    errors: list[str] = []
    raw_path = values.get("MUYE_DATA_CONFIG_PATH", "config.yaml").strip()
    if not raw_path:
        errors.append("MUYE_DATA_CONFIG_PATH 不能为空")
    else:
        config_path = Path(raw_path)
        if not config_path.is_absolute():
            config_path = project_root / "muye-data" / config_path
        if not config_path.is_file():
            errors.append(f"MUYE_DATA_CONFIG_PATH 指向的文件不存在：{config_path}")

    llm_base_url = values.get("MUYE_DATA_LLM_BASE_URL", "http://127.0.0.1:9850").strip()
    if not _is_http_url(llm_base_url):
        errors.append("MUYE_DATA_LLM_BASE_URL 必须是有效的 HTTP(S) URL")
    return errors


def print_configuration_errors(errors: Sequence[str]) -> None:
    """兼容旧调用方的配置提示；根启动器自身不调用此函数。"""
    print(_c(RED + BOLD, "\n启动前配置检查未通过，尚未启动任何服务："))
    for error in errors:
        print(f"  - {error}")
    print("请检查对应模块目录中的 .env 文件。真实密钥不得提交到版本控制。", flush=True)


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
    try:
        _, port, health_url = service_runtime_address(svc)
    except ValueError as exc:
        print(_c(RED, f"  ✘ {name} 配置无效: {exc}"), flush=True)
        return None

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
def dry_run(active_services: Sequence[Mapping[str, object]] | None = None) -> None:
    """检查服务入口与启用状态，但不读取或校验模块业务配置。"""
    print(f"\n{_c(BOLD + YELLOW, '⚡ DRY-RUN 模式')}\n")
    python_bin = Path(PYTHON_BIN)
    print(f"  Python (.venv): {_c(BLUE, str(python_bin))} {'✔' if python_bin.exists() else '✘'}\n")
    all_ok = python_bin.exists()
    active_ids = {
        service.get("id")
        for service in (active_services if active_services is not None else enabled_services())
    }
    for svc in SERVICES:
        cwd = PROJECT_ROOT / svc["cwd"]
        cmd_file = cwd / svc["cmd"][1]
        ok_cwd = cwd.exists()
        ok_cmd = cmd_file.exists()
        ok_py = Path(svc["cmd"][0]).exists()
        status = _c(GREEN, "✔") if (ok_cwd and ok_cmd and ok_py) else _c(RED, "✘")
        print(f"  {status} {svc['name']}")
        try:
            _, port, health_url = service_runtime_address(svc)
        except ValueError as exc:
            print(_c(RED, f"      运行地址配置无效: {exc}"))
            all_ok = False
        else:
            print(f"      健康检查: {health_url} (端口 {port})")
        if svc.get("enabled_env") and svc.get("id") not in active_ids:
            print(_c(YELLOW, f"      已由 {svc['enabled_env']}=false 跳过运行"))
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


# ─── 关闭 ─────────────────────────────────────────────────────────────────────
def _graceful_shutdown(sig, frame):
    print(f"\n\n{_c(BOLD + YELLOW, '⚡ 正在关闭服务...')}", flush=True)
    for proc in reversed(_started_procs):
        _terminate_proc(proc)
    print(_c(BOLD + GREEN, "✅ 服务已停止"), flush=True)
    sys.exit(0)


# ─── 启动完成提示 ───────────────────────────────────────────────────────────
def _print_ready_guide(services: Sequence[Mapping[str, object]]) -> None:
    data_port = "  9840 — muye-data（只读召回）\n" if any(service.get("id") == 6 for service in services) else ""
    guide = f"""
{_c(BOLD + GREEN, "═" * 68)}
{_c(BOLD + GREEN, "  ✅ Muye 服务已就绪！")}

{_c(BOLD, "服务端口：")}
  9850 — muye-llm
{data_port}  9860 — Main Agent
  9870 — Gateway 内部认证与状态 API

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

    services = enabled_services()

    if args.dry_run:
        dry_run(services)
        return

    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    print(f"\n{_c(BOLD + GREEN, '═' * 68)}")
    print(f"{_c(BOLD + GREEN, '  Muye Multi-Agent 服务组')}")
    print(f"{_c(BOLD + GREEN, '═' * 68)}\n")

    procs = []
    for svc in services:
        res = start_service(svc, args.timeout)
        if res is None:
            print(_c(RED, "\n✘ 启动失败"))
            _graceful_shutdown(None, None)
        procs.append(res)

    _print_ready_guide(services)

    while True:
        for svc, p in zip(services, procs):
            if isinstance(p, _AlreadyRunningProc): continue
            if p.poll() is not None:
                print(_c(RED, f"\n⚠ {svc['name']} 已退出"))
        time.sleep(5)


if __name__ == "__main__":
    main()
