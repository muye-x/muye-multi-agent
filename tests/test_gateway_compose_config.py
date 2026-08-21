"""Gateway HTTPS 与核心 Compose 连接边界回归测试。"""

from __future__ import annotations

from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_gateway_proxies_control_api_only_from_https_server() -> None:
    source = (
        PROJECT_ROOT / "muye-gateway" / "nginx" / "conf.d" / "muye-gateway.conf.template"
    ).read_text(encoding="utf-8")
    http_server, https_server = source.split("server {", 2)[1:]

    assert "location /api/v2/" not in http_server
    assert "return 301 https://$host$request_uri" in http_server
    assert "location /api/v2/" in https_server
    assert "proxy_pass ${MUYE_CONTROL_BASE_URL}/api/v2/;" in https_server


def test_core_compose_mounts_tls_and_provides_agent_dependencies() -> None:
    compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    gateway = compose["services"]["gateway"]
    data = compose["services"]["muye-data"]
    agent_main = compose["services"]["agent-main"]

    assert "${MUYE_GATEWAY_TLS_CERTIFICATE_PATH:-/dev/null}:/etc/nginx/tls/tls.crt:ro" in gateway["volumes"]
    assert "${MUYE_GATEWAY_TLS_PRIVATE_KEY_PATH:-/dev/null}:/etc/nginx/tls/tls.key:ro" in gateway["volumes"]
    assert data["environment"]["MUYE_DATA_LLM_BASE_URL"] == "http://muye-llm:9850"
    assert agent_main["environment"]["MUYE_AGENT_HOST"] == "0.0.0.0"
    assert agent_main["environment"]["MUYE_SDK_DATA_BASE_URL"] == "http://muye-data:9840"
    assert "MUYE_SERVER_HOST" not in agent_main["environment"]
    assert agent_main["build"] == {
        "context": ".",
        "dockerfile": "agents/agent-main/Dockerfile",
    }
    assert agent_main["command"] == ["python", "main.py"]

    for service_name in (
        "postgres",
        "control",
        "dashboard-api",
        "agent-main",
        "muye-llm",
        "muye-data",
        "gateway",
    ):
        assert compose["services"][service_name]["env_file"][0]["required"] is False


def test_compose_management_commands_allow_missing_module_environment_files() -> None:
    source = (PROJECT_ROOT / "scripts" / "muye.sh").read_text(encoding="utf-8")

    assert "require_compose_environment" in source
    assert "up) shift; compose_up up -d" in source
    assert "down) shift; compose_manage down" in source
    assert "MUYE_GATEWAY_TLS_CERTIFICATE_PATH=/dev/null" in source


def test_gateway_supports_explicit_http_mode_without_tls_redirect() -> None:
    source = (
        PROJECT_ROOT / "muye-gateway" / "nginx" / "conf.d" / "muye-gateway.http.conf.template"
    ).read_text(encoding="utf-8")

    assert "listen 80;" in source
    assert "return 301 https://" not in source
    assert "X-Forwarded-Proto http" in source
    assert "rewrite ^/api/v2/channels/(.*)$ /api/v1/$1 break;" in source
    assert "proxy_pass $channels_upstream;" in source


def test_generated_agent_compose_uses_internal_llm_and_data_urls() -> None:
    source = (PROJECT_ROOT / "tools" / "agent_catalog" / "generator.py").read_text(encoding="utf-8")

    assert '"MUYE_LLM_BASE_URL": "http://muye-llm:9850"' in source
    assert '"MUYE_SDK_DATA_BASE_URL": "http://muye-data:9840"' in source


def test_agent_main_image_copies_repository_contracts_package() -> None:
    source = (PROJECT_ROOT / "agents" / "agent-main" / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert "COPY contracts ./contracts" in source
    assert "COPY agents/agent-main ./agent-main" in source
    assert "PYTHONPATH=/app" in source

    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert "**/.env" in dockerignore
    assert "!**/.env.example" in dockerignore
