#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd -- "${script_directory}/.." && pwd)"
python_executable="${workspace_root}/.venv/bin/python"

if [[ ! -x "${python_executable}" ]]; then
  printf 'error: Scaffold Python environment is unavailable: %s\n' "${python_executable}" >&2
  exit 2
fi

cd "${workspace_root}"
exec "${python_executable}" -m tools.cli "$@"
