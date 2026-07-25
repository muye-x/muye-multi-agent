#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="${MUYE_DASHBOARD_PYTHON:-python}"
host="${MUYE_DASHBOARD_HOST:-127.0.0.1}"
port="${MUYE_DASHBOARD_PORT:-9870}"

cd "${root_dir}"
exec env MUYE_DASHBOARD_HOST="${host}" MUYE_DASHBOARD_PORT="${port}" \
  "${python_bin}" dashboard_main.py
