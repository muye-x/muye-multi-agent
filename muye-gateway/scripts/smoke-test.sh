#!/usr/bin/env bash
set -euo pipefail
: "${MUYE_GATEWAY_BASE_URL:?Missing MUYE_GATEWAY_BASE_URL}"
curl -fsS "${MUYE_GATEWAY_BASE_URL}/gateway/health" >/dev/null
test "$(curl -s -o /dev/null -w '%{http_code}' "${MUYE_GATEWAY_BASE_URL}/agentMain/health")" = "401"
