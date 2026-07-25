#!/usr/bin/env bash
set -euo pipefail
: "${MUYE_GATEWAY_BASE_URL:?Missing MUYE_GATEWAY_BASE_URL}"
: "${MUYE_GATEWAY_API_KEY:?Missing MUYE_GATEWAY_API_KEY}"
curl -fsS "${MUYE_GATEWAY_BASE_URL}/gateway/health" >/dev/null
test "$(curl -s -o /dev/null -w '%{http_code}' "${MUYE_GATEWAY_BASE_URL}/agentMain/health")" = "401"
curl -fsS -H "Authorization: Bearer ${MUYE_GATEWAY_API_KEY}" "${MUYE_GATEWAY_BASE_URL}/agentMain/health" >/dev/null
