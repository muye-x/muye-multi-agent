"""运维配置契约的回归测试。"""

from __future__ import annotations

from pathlib import Path

from tools import operations


_TOKEN_NAMES = (
    "MUYE_CONTROL_DATABASE_URL",
    "MUYE_CONTROL_OPERATOR_TOKEN",
    "MUYE_CONTROL_MAIN_TOKEN",
    "MUYE_CONTROL_HEALTH_TOKEN",
    "MUYE_CONTROL_GATEWAY_TOKEN",
    "MUYE_MAIN_CALLER_TOKEN",
    "MUYE_GATEWAY_CONTROL_TOKEN",
)


def _write_environment_files(root: Path, *, main_token: str = "main-token", gateway_token: str = "gateway-token", caller_token: str = "caller-token") -> None:
    """创建最小、无真实凭据的跨模块配置。"""

    control = root / "control_server"
    agent_main = root / "agents" / "agent-main"
    gateway = root / "muye-gateway"
    control.mkdir(parents=True)
    agent_main.mkdir(parents=True)
    gateway.mkdir(parents=True)
    (control / ".env").write_text(
        "\n".join(
            (
                "MUYE_CONTROL_DATABASE_URL=postgresql://example.test/muye",
                "MUYE_CONTROL_OPERATOR_TOKEN=operator-token",
                f"MUYE_CONTROL_MAIN_TOKEN={main_token}",
                "MUYE_CONTROL_HEALTH_TOKEN=health-token",
                f"MUYE_CONTROL_GATEWAY_TOKEN={gateway_token}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (agent_main / ".env").write_text(
        f"MUYE_CONTROL_MAIN_TOKEN={main_token}\nMUYE_MAIN_CALLER_TOKEN={caller_token}\n",
        encoding="utf-8",
    )
    (gateway / ".env").write_text(
        f"MUYE_GATEWAY_CONTROL_TOKEN={gateway_token}\nMUYE_MAIN_CALLER_TOKEN={caller_token}\n",
        encoding="utf-8",
    )


def _clear_token_environment(monkeypatch) -> None:
    for name in _TOKEN_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_doctor_accepts_required_shared_service_tokens(tmp_path: Path, monkeypatch) -> None:
    _clear_token_environment(monkeypatch)
    _write_environment_files(tmp_path)
    monkeypatch.setattr(operations, "_compose", lambda root, arguments: 0)

    assert operations.doctor(tmp_path) == 0


def test_doctor_rejects_main_token_mismatch(tmp_path: Path, monkeypatch, capsys) -> None:
    _clear_token_environment(monkeypatch)
    _write_environment_files(tmp_path)
    (tmp_path / "agents" / "agent-main" / ".env").write_text(
        "MUYE_CONTROL_MAIN_TOKEN=other-main-token\nMUYE_MAIN_CALLER_TOKEN=caller-token\n",
        encoding="utf-8",
    )

    assert operations.doctor(tmp_path) == 2
    assert "MUYE_CONTROL_MAIN_TOKEN must match agents/agent-main/.env:MUYE_CONTROL_MAIN_TOKEN" in capsys.readouterr().out


def test_doctor_rejects_gateway_token_mismatch(tmp_path: Path, monkeypatch, capsys) -> None:
    _clear_token_environment(monkeypatch)
    _write_environment_files(tmp_path)
    (tmp_path / "muye-gateway" / ".env").write_text(
        "MUYE_GATEWAY_CONTROL_TOKEN=other-gateway-token\nMUYE_MAIN_CALLER_TOKEN=caller-token\n",
        encoding="utf-8",
    )

    assert operations.doctor(tmp_path) == 2
    assert "MUYE_CONTROL_GATEWAY_TOKEN must match muye-gateway/.env:MUYE_GATEWAY_CONTROL_TOKEN" in capsys.readouterr().out


def test_doctor_rejects_reused_logical_token(tmp_path: Path, monkeypatch, capsys) -> None:
    _clear_token_environment(monkeypatch)
    _write_environment_files(tmp_path, caller_token="main-token")

    assert operations.doctor(tmp_path) == 2
    assert "tokens must be distinct" in capsys.readouterr().out
