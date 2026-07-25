#!/usr/bin/env bash
set -euo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
config="${1:-${root_dir}/build/nginx/conf.d/muye-gateway.conf}"
[[ -f "${config}" ]] || { echo "Missing rendered config: ${config}" >&2; exit 1; }
nginx -t -c "${config}"
