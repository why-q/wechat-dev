#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE=wechat-dev-test.service
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
ROOT_ENV="$REPO_ROOT/.env"
RAG_ENV="$REPO_ROOT/rag-poc/.env"

fail() {
    printf 'restart failed: %s\n' "$1" >&2
    exit 1
}

if (( EUID != 0 )); then
    fail 'run with sudo: sudo ./restart-wechat-dev-test.sh'
fi

for command in systemctl journalctl curl date; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
PYTHON="$REPO_ROOT/rag-poc/.venv/bin/python"
[[ -x "$PYTHON" ]] || fail "required Python interpreter is missing: $PYTHON"

verify_loaded_environment() {
    "$PYTHON" -c '
import hashlib
import hmac
import sys
from dotenv import dotenv_values

root_env, rag_env, pid = sys.argv[1:]
expected = {}
for path in (root_env, rag_env):
    expected.update({key: value for key, value in dotenv_values(path).items() if value is not None})
try:
    raw = open(f"/proc/{pid}/environ", "rb").read()
except OSError:
    print("restart failed: cannot read service environment", file=sys.stderr)
    raise SystemExit(1)
actual = {key.decode(): value for key, value in (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)}
keys = sorted(expected)
if any(key not in actual for key in keys):
    print("restart failed: a configured EnvironmentFile variable is absent from the service", file=sys.stderr)
    raise SystemExit(1)
def fingerprint(values):
    payload = b"\0".join(key.encode() + b"=" + values[key] for key in keys)
    return hashlib.sha256(payload).digest()
expected_bytes = {key: value.encode() for key, value in expected.items()}
if not hmac.compare_digest(fingerprint(expected_bytes), fingerprint(actual)):
    print("restart failed: loaded service environment does not match both .env files", file=sys.stderr)
    raise SystemExit(1)
print("environment_fingerprint=match")
' "$ROOT_ENV" "$RAG_ENV" "$1"
}


for path in "$REPO_ROOT/wechat-dev-test.service" "$ROOT_ENV" "$RAG_ENV"; do
    [[ -r "$path" ]] || fail "required service/config file is not readable: $path"
done

printf 'Reloading systemd unit definitions...\n'
systemctl daemon-reload

need_reload="$(systemctl show "$SERVICE" --property=NeedDaemonReload --value)"
[[ "$need_reload" == no ]] || fail "systemd still reports NeedDaemonReload=$need_reload"

environment_files="$(systemctl show "$SERVICE" --property=EnvironmentFiles --value)"
[[ "$environment_files" == *"$ROOT_ENV"* ]] || fail 'loaded unit does not include the repository root .env'
[[ "$environment_files" == *"$RAG_ENV"* ]] || fail 'loaded unit does not include rag-poc/.env'

previous_main_pid="$(systemctl show "$SERVICE" --property=MainPID --value)"
[[ "$previous_main_pid" =~ ^[1-9][0-9]*$ ]] || fail 'service has no running main process before restart'
started_at="$(date '+%Y-%m-%d %H:%M:%S')"
printf 'Restarting %s and re-reading EnvironmentFile values...\n' "$SERVICE"
systemctl restart "$SERVICE"

deadline=$((SECONDS + 60))
while (( SECONDS < deadline )); do
    active="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    [[ "$active" == active ]] || fail "service state after restart: ${active:-unknown}"

    startup_log="$(journalctl --unit "$SERVICE" --since "$started_at" --output=cat --no-pager 2>/dev/null || true)"
    [[ "$startup_log" != *'RAG startup failed'* ]] || fail 'RAG responder initialization failed; inspect journalctl'

    if [[ "$startup_log" == *'Application startup complete.'* ]]; then
        main_pid="$(systemctl show "$SERVICE" --property=MainPID --value)"
        [[ "$main_pid" =~ ^[1-9][0-9]*$ ]] || fail 'service has no running main process'
        [[ "$main_pid" != "$previous_main_pid" ]] || fail 'service MainPID did not change after restart'
        verify_loaded_environment "$main_pid" || fail 'service environment verification failed'
        health="$(curl --connect-timeout 1 --max-time 3 --fail --silent http://127.0.0.1:8000/healthz 2>/dev/null || true)"
        if [[ "$health" == ok ]]; then
            printf 'restart verified: service=active pid=%s changed=yes environment=match rag=initialized healthz=ok\n' "$main_pid"
            exit 0
        fi
    fi
    sleep 1
done

fail 'service did not complete RAG startup and pass /healthz within 60 seconds; inspect journalctl'
