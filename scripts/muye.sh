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

compose_files=(-f compose.yaml -f compose.agents.generated.yaml)
compose() {
  docker compose --project-name "${MUYE_COMPOSE_PROJECT_NAME:-muye}" "${compose_files[@]}" "$@"
}

case "${1:-}" in
  init)
    admin_username="${MUYE_CONTROL_BOOTSTRAP_ADMIN_USERNAME:-}"
    admin_password="${MUYE_CONTROL_BOOTSTRAP_ADMIN_PASSWORD:-}"
    if [[ -z "${admin_username}" ]]; then read -r -p "Initial admin username: " admin_username; fi
    if [[ -z "${admin_password}" ]]; then read -r -s -p "Initial admin password: " admin_password; printf '\n'; fi
    if [[ -z "${admin_username}" || -z "${admin_password}" ]]; then
      printf 'error: initial admin username and password are required\n' >&2
      exit 2
    fi
    MUYE_CONTROL_BOOTSTRAP_ADMIN_USERNAME="${admin_username}" MUYE_CONTROL_BOOTSTRAP_ADMIN_PASSWORD="${admin_password}" \
      exec "${python_executable}" -m control_server.bootstrap
    ;;
  up) shift; exec compose up -d "$@" ;;
  down) shift; exec compose down "$@" ;;
  restart) shift; compose down; exec compose up -d "$@" ;;
  status) shift; exec compose ps "$@" ;;
  logs) shift; exec compose logs --tail=200 "$@" ;;
  doctor) shift; exec "${python_executable}" -m tools.operations doctor "$@" ;;
  smoke) shift; exec "${python_executable}" -m tools.operations smoke "$@" ;;
  *) exec "${python_executable}" -m tools.cli "$@" ;;
esac
