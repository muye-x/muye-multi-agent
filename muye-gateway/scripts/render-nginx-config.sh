#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
template="${root_dir}/nginx/conf.d/muye-gateway.conf.template"
output_dir="${root_dir}/build/nginx/conf.d"
if [[ -f "${root_dir}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "${root_dir}/.env"
  set +a
fi
for name in MUYE_GATEWAY_API_KEY MUYE_GATEWAY_SERVER_NAME MUYE_GATEWAY_TLS_CERTIFICATE MUYE_GATEWAY_TLS_PRIVATE_KEY; do
  [[ -n "${!name:-}" ]] || { echo "Missing ${name}" >&2; exit 1; }
done
export MUYE_AGENT_MAIN_URL="${MUYE_AGENT_MAIN_URL:-http://127.0.0.1:9860}"
export MUYE_AGENT_TRAVEL_URL="${MUYE_AGENT_TRAVEL_URL:-http://127.0.0.1:8011}"
export MUYE_DASHBOARD_API_URL="${MUYE_DASHBOARD_API_URL:-http://127.0.0.1:9870}"
export MUYE_GATEWAY_DASHBOARD_ROOT="${MUYE_GATEWAY_DASHBOARD_ROOT:-${root_dir}/dashboard/web}"
export MUYE_GATEWAY_CLIENT_MAX_BODY_SIZE="${MUYE_GATEWAY_CLIENT_MAX_BODY_SIZE:-50m}"
export MUYE_GATEWAY_PROXY_READ_TIMEOUT="${MUYE_GATEWAY_PROXY_READ_TIMEOUT:-3600s}"
export MUYE_GATEWAY_PROXY_SEND_TIMEOUT="${MUYE_GATEWAY_PROXY_SEND_TIMEOUT:-3600s}"
mkdir -p "${output_dir}"
envsubst < "${template}" > "${output_dir}/muye-gateway.conf"
chmod 600 "${output_dir}/muye-gateway.conf"
