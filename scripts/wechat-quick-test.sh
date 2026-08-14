#!/usr/bin/env bash
set -euo pipefail

script_directory="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd -- "${script_directory}/.." && pwd)"
python_executable="${workspace_root}/../.venv/bin/python"
uvicorn_executable="${workspace_root}/../.venv/bin/uvicorn"
user_id="${1:-local-user-001}"
channels_port="${MUYE_WECHAT_TEST_PORT:-9890}"
postgres_port="${MUYE_WECHAT_TEST_POSTGRES_PORT:-55432}"
runtime_root="${MUYE_WECHAT_TEST_RUNTIME_DIR:-/tmp/muye-wechat-quick-test}"
postgres_root="${runtime_root}/postgres"
channels_log="${runtime_root}/channels.log"
postgres_started=false

if [[ ! -x "${python_executable}" || ! -x "${uvicorn_executable}" ]]; then
  printf 'error: Scaffold Python environment is unavailable: %s\n' "${workspace_root}/../.venv" >&2
  exit 2
fi
if ! command -v curl >/dev/null 2>&1; then
  printf 'error: curl is required\n' >&2
  exit 2
fi
if [[ ! -x /usr/lib/postgresql/16/bin/initdb || ! -x /usr/lib/postgresql/16/bin/pg_ctl ]]; then
  printf 'error: PostgreSQL 16 server binaries are required\n' >&2
  exit 2
fi

mkdir -p "${runtime_root}"
if [[ ! -f "${postgres_root}/PG_VERSION" ]]; then
  /usr/lib/postgresql/16/bin/initdb -D "${postgres_root}" --no-locale --encoding=UTF8 >/dev/null
fi

cleanup() {
  if [[ -n "${channels_pid:-}" ]] && kill -0 "${channels_pid}" 2>/dev/null; then
    kill "${channels_pid}" 2>/dev/null || true
    wait "${channels_pid}" 2>/dev/null || true
  fi
  if [[ "${postgres_started}" == true ]]; then
    /usr/lib/postgresql/16/bin/pg_ctl -D "${postgres_root}" -m fast -w stop >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

if ! pg_isready -h 127.0.0.1 -p "${postgres_port}" >/dev/null 2>&1; then
  /usr/lib/postgresql/16/bin/pg_ctl -D "${postgres_root}" \
    -o "-p ${postgres_port} -h 127.0.0.1 -k ${runtime_root}" \
    -l "${runtime_root}/postgres.log" -w start >/dev/null
  postgres_started=true
fi
createdb_command="${python_executable}"
"${createdb_command}" - <<PY
import psycopg
with psycopg.connect("postgresql://127.0.0.1:${postgres_port}/postgres") as connection:
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = 'muye'")
        if cursor.fetchone() is None:
            cursor.execute("CREATE DATABASE muye")
PY

caller_token="${MUYE_WECHAT_TEST_CALLER_TOKEN:-local-wechat-caller-token-12345}"
main_token="${MUYE_WECHAT_TEST_MAIN_TOKEN:-local-wechat-main-token-12345}"
encryption_key="${MUYE_WECHAT_TEST_ENCRYPTION_KEY:-$(${python_executable} -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')}"

cd "${workspace_root}/muye-channels"
MUYE_CHANNELS_CALLER_TOKEN="${caller_token}" \
MUYE_CHANNELS_MAIN_TOKEN="${main_token}" \
MUYE_CHANNELS_ENCRYPTION_KEY="${encryption_key}" \
MUYE_CHANNELS_DATABASE_URL="postgresql://127.0.0.1:${postgres_port}/muye" \
MUYE_CHANNELS_MAIN_URL="${MUYE_CHANNELS_MAIN_URL:-http://127.0.0.1:9860}" \
WECHAT_ILINK_ALLOWED_HOSTS="${WECHAT_ILINK_ALLOWED_HOSTS:-ilinkai.weixin.qq.com}" \
PYTHONPATH="${workspace_root}/muye-channels:${workspace_root}/../sdk/src" \
"${uvicorn_executable}" main:create_app --factory --host 127.0.0.1 --port "${channels_port}" >"${channels_log}" 2>&1 &
channels_pid=$!

for _ in {1..30}; do
  if curl --noproxy '*' -fsS "http://127.0.0.1:${channels_port}/health" >/dev/null; then break; fi
  sleep 1
done
if ! curl --noproxy '*' -fsS "http://127.0.0.1:${channels_port}/health" >/dev/null; then
  printf 'error: muye-channels failed to start; see %s\n' "${channels_log}" >&2
  exit 1
fi

response="$(curl --noproxy '*' -fsS -X POST "http://127.0.0.1:${channels_port}/api/v1/bindings/wechat/qrcode" \
  -H "Authorization: Bearer ${caller_token}" \
  -H "X-Muye-User-Id: ${user_id}" \
  -H 'Content-Type: application/json' --data '{}')"
session_id="$("${python_executable}" -c 'import json,sys; print(json.loads(sys.argv[1])["session_id"])' "${response}")"
"${python_executable}" - "${response}" "${postgres_port}" "${user_id}" "${encryption_key}" <<'PY'
import base64
import json
import sys

import psycopg

payload = json.loads(sys.argv[1])
session_id = payload["session_id"]
with psycopg.connect(f"postgresql://127.0.0.1:{sys.argv[2]}/muye") as connection:
    with connection.cursor() as cursor:
        cursor.execute("SELECT qr_content FROM channel_qr_sessions WHERE session_id = %s AND user_id = %s", (session_id, sys.argv[3]))
        row = cursor.fetchone()
if row is None:
    raise SystemExit("error: QR session was not persisted")
print(f"user_id: {sys.argv[3]}")
print(f"session_id: {session_id}")
print(f"wechat_qr_url: {row[0]}")
print("请使用微信扫描上面的链接对应二维码，并在手机中确认。脚本将自动轮询绑定状态。")
PY

status_url="http://127.0.0.1:${channels_port}/api/v1/bindings/wechat/qrcode/${session_id}"
last_status=""
while true; do
  status_response="$(curl --noproxy '*' -fsS "${status_url}" \
    -H "Authorization: Bearer ${caller_token}" \
    -H "X-Muye-User-Id: ${user_id}")"
  status="$("${python_executable}" -c 'import json,sys; print(json.loads(sys.argv[1]).get("status", ""))' "${status_response}")"
  if [[ "${status}" != "${last_status}" ]]; then
    printf 'wechat_status: %s\n' "${status}"
    last_status="${status}"
  fi
  case "${status}" in
    confirmed)
      printf 'wechat_binding: active\n'
      break
      ;;
    need_verifycode)
      read -r -p '请输入微信验证码: ' verify_code
      curl --noproxy '*' -fsS -X POST "${status_url}/verify" \
        -H "Authorization: Bearer ${caller_token}" \
        -H "X-Muye-User-Id: ${user_id}" \
        -H 'Content-Type: application/json' \
        --data "$("${python_executable}" -c 'import json,sys; print(json.dumps({"verify_code": sys.argv[1]}))' "${verify_code}")" >/dev/null
      ;;
    expired|verify_code_blocked)
      printf 'error: 微信二维码已过期，请重新运行此脚本。\n' >&2
      exit 1
      ;;
  esac
  sleep 1.5
done

wait "${channels_pid}"
