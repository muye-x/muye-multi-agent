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

    assert "${MUYE_GATEWAY_TLS_CERTIFICATE_PATH:?set MUYE_GATEWAY_TLS_CERTIFICATE_PATH}:/etc/nginx/tls/tls.crt:ro" in gateway["volumes"]
    assert "${MUYE_GATEWAY_TLS_PRIVATE_KEY_PATH:?set MUYE_GATEWAY_TLS_PRIVATE_KEY_PATH}:/etc/nginx/tls/tls.key:ro" in gateway["volumes"]
    assert data["environment"]["MUYE_DATA_LLM_BASE_URL"] == "http://muye-llm:9850"


def test_generated_agent_compose_uses_internal_llm_and_data_urls() -> None:
    source = (PROJECT_ROOT / "tools" / "agent_catalog" / "generator.py").read_text(encoding="utf-8")

    assert '"MUYE_LLM_BASE_URL": "http://muye-llm:9850"' in source
    assert '"MUYE_SDK_DATA_BASE_URL": "http://muye-data:9840"' in source
