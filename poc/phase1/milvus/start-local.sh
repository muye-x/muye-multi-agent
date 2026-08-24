#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd -- "${script_directory}/../../.." && pwd)"
python_executable="${workspace_root}/.venv/bin/python"
compose_file="${script_directory}/compose.yaml"
environment_file="${script_directory}/.env"
# Root Compose connects muye-data to the external ``milvus_default`` network
# and resolves the standalone service as ``milvus-milvus-1``. Keep the local
# project name aligned so this helper starts the exact dependency it declares.
project_name="milvus"

if [[ ! -x "${python_executable}" ]]; then
  printf 'error: Scaffold Python environment is unavailable: %s\n' "${python_executable}" >&2
  exit 2
fi
if ! command -v docker >/dev/null; then
  printf 'error: Docker is required to start local Milvus\n' >&2
  exit 2
fi

write_environment_file() {
  local minio_user minio_password credentials
  if [[ -n "${PHASE1_MINIO_ROOT_USER:-}" || -n "${PHASE1_MINIO_ROOT_PASSWORD:-}" ]]; then
    if [[ -z "${PHASE1_MINIO_ROOT_USER:-}" || -z "${PHASE1_MINIO_ROOT_PASSWORD:-}" ]]; then
      printf 'error: set both PHASE1_MINIO_ROOT_USER and PHASE1_MINIO_ROOT_PASSWORD, or neither\n' >&2
      return 1
    fi
    minio_user="${PHASE1_MINIO_ROOT_USER}"
    minio_password="${PHASE1_MINIO_ROOT_PASSWORD}"
  else
    credentials="$("${python_executable}" -c 'import secrets; print(f"local-{secrets.token_urlsafe(12)} {secrets.token_urlsafe(32)}")')"
    read -r minio_user minio_password <<<"${credentials}"
  fi

  (
    umask 077
    printf 'PHASE1_MINIO_ROOT_USER=%s\nPHASE1_MINIO_ROOT_PASSWORD=%s\n' \
      "${minio_user}" "${minio_password}" >"${environment_file}"
  )
  printf 'created local MinIO credentials: %s\n' "${environment_file}"
}

if [[ -L "${environment_file}" || ( -e "${environment_file}" && ! -f "${environment_file}" ) ]]; then
  printf 'error: local MinIO credentials must be a regular file: %s\n' "${environment_file}" >&2
  exit 2
fi
if [[ ! -f "${environment_file}" ]]; then
  write_environment_file
fi
if ! grep -qE '^PHASE1_MINIO_ROOT_USER=.+$' "${environment_file}" \
  || ! grep -qE '^PHASE1_MINIO_ROOT_PASSWORD=.+$' "${environment_file}"; then
  printf 'error: local MinIO credentials are incomplete: %s\n' "${environment_file}" >&2
  exit 2
fi
chmod 600 "${environment_file}"

env -u PHASE1_MINIO_ROOT_USER -u PHASE1_MINIO_ROOT_PASSWORD \
  docker compose --env-file "${environment_file}" \
    -f "${compose_file}" \
    -p "${project_name}" \
    up -d

for attempt in $(seq 1 30); do
  if (echo >/dev/tcp/127.0.0.1/19530) 2>/dev/null; then
    printf 'local Milvus is ready on 127.0.0.1:19530\n'
    exit 0
  fi
  sleep 1
done

printf 'error: Milvus did not accept connections on 127.0.0.1:19530\n' >&2
exit 1
