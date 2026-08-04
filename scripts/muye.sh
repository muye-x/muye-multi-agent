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
compose_environment_paths=(
  control_server/.env
  muye-llm/.env
  muye-data/.env
  agents/agent-main/.env
  muye-gateway/.env
)
compose_environment_arguments=()
for environment_path in "${compose_environment_paths[@]}"; do
  if [[ -f "${environment_path}" ]]; then
    compose_environment_arguments+=(--env-file "${environment_path}")
  fi
done

require_compose_environment() {
  local missing=()
  local environment_path
  for environment_path in "${compose_environment_paths[@]}"; do
    if [[ ! -f "${environment_path}" ]]; then missing+=("${environment_path}"); fi
  done
  if (( ${#missing[@]} > 0 )); then
    printf 'error: missing module environment files: %s\n' "${missing[*]}" >&2
    return 2
  fi
}

compose_up() {
  require_compose_environment
  docker compose --project-name "${MUYE_COMPOSE_PROJECT_NAME:-muye}" "${compose_environment_arguments[@]}" "${compose_files[@]}" "$@"
}

compose_manage() {
  MUYE_GATEWAY_TLS_CERTIFICATE_PATH=/dev/null \
  MUYE_GATEWAY_TLS_PRIVATE_KEY_PATH=/dev/null \
    docker compose --project-name "${MUYE_COMPOSE_PROJECT_NAME:-muye}" "${compose_environment_arguments[@]}" "${compose_files[@]}" "$@"
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
  up) shift; compose_up up -d "$@" ;;
  down) shift; compose_manage down "$@" ;;
  restart) shift; compose_manage down; compose_up up -d "$@" ;;
  status) shift; compose_manage ps "$@" ;;
  logs) shift; compose_manage logs --tail=200 "$@" ;;
  doctor) shift; exec "${python_executable}" -m tools.operations doctor "$@" ;;
  smoke) shift; exec "${python_executable}" -m tools.operations smoke "$@" ;;
  *) exec "${python_executable}" -m tools.cli "$@" ;;
esac
