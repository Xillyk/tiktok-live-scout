#!/usr/bin/env bash
# Double-click in Finder (or `bash tiktok-live-scout-v2.command`) to run the
# v2 stack: Postgres + Adminer + one v2 scout process per target listed in
# config_v2.yaml. No TikTok login involved (v2 is anonymous WebSocket).
#
# Ctrl+C in this terminal stops every v2 scout. Postgres + Adminer stay up
# (use `docker compose down` to stop them).

set -uo pipefail
cd "$(dirname "$0")"

print() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err()   { printf '\033[1;31m!!!\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------- venv
if [ ! -d .venv ]; then
    print "First-run setup: creating Python venv and installing deps…"
    if ! command -v python3 >/dev/null 2>&1; then
        err "python3 not found. Install Python from https://python.org first."
        read -r -p "Press Enter to close…"; exit 1
    fi
    python3 -m venv .venv
    .venv/bin/pip install --quiet --upgrade pip
    .venv/bin/pip install --quiet -r requirements.txt
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# ---------------------------------------------------------------- Docker
if ! docker info >/dev/null 2>&1; then
    print "Starting Docker Desktop (may take ~20s)…"
    if ! open -a Docker 2>/dev/null; then
        err "Docker Desktop isn't installed. Get it from https://docker.com."
        read -r -p "Press Enter to close…"; exit 1
    fi
    until docker info >/dev/null 2>&1; do sleep 1; done
fi

# ---------------------------------------------------------------- Postgres + Adminer
print "Starting Postgres + Adminer…"
docker compose up -d postgres adminer >/dev/null

until [ "$(docker inspect -f '{{.State.Health.Status}}' tiktok-live-scout-db 2>/dev/null)" = "healthy" ]; do
    sleep 1
done
print "Postgres healthy."

ADMINER_PORT=$(docker compose port adminer 8080 2>/dev/null | awk -F: '{print $NF}')
ADMINER_URL="http://127.0.0.1:${ADMINER_PORT}"

V2_WEB_HOST="127.0.0.1"
V2_WEB_PORT="8767"
V2_WEB_URL="http://${V2_WEB_HOST}:${V2_WEB_PORT}"

# ---------------------------------------------------------------- read targets
CONFIG="config_v2.yaml"
if [ ! -f "$CONFIG" ]; then
    err "$CONFIG not found"
    read -r -p "Press Enter to close…"; exit 1
fi

# bash 3.2 on macOS doesn't have mapfile, so loop into an array.
TARGETS=()
while IFS= read -r t; do
    [ -n "$t" ] && TARGETS+=("$t")
done < <(python -c "
import sys, yaml
cfg = yaml.safe_load(open('$CONFIG')) or {}
for t in (cfg.get('targets') or []):
    if isinstance(t, str) and t.strip():
        print(t.strip())
" 2>/dev/null)

if [ "${#TARGETS[@]}" -eq 0 ]; then
    err "No targets found in $CONFIG. Add some under the 'targets:' list."
    read -r -p "Press Enter to close…"; exit 1
fi

print "Targets (${#TARGETS[@]}): ${TARGETS[*]}"

# ---------------------------------------------------------------- replace any prior v2 scouts + web
print "Cleaning up any previous v2 scout + web instances…"
pkill -f 'src\.v2\.(scout|web)' 2>/dev/null || true
sleep 1
pkill -9 -f 'src\.v2\.(scout|web)' 2>/dev/null || true

# ---------------------------------------------------------------- launch scouts
mkdir -p logs/v2
PIDS=()

cleanup() {
    echo
    print "Stopping v2 scouts…"
    if [ "${#PIDS[@]}" -gt 0 ]; then
        kill "${PIDS[@]}" 2>/dev/null || true
        # Give them a couple of seconds to flush + close the session.
        for _ in 1 2 3 4 5; do
            sleep 1
            still=0
            for p in "${PIDS[@]}"; do
                kill -0 "$p" 2>/dev/null && still=1
            done
            [ "$still" -eq 0 ] && break
        done
        kill -9 "${PIDS[@]}" 2>/dev/null || true
    fi
    print "Postgres + Adminer left running. \`docker compose down\` to stop them."
}
trap cleanup EXIT INT TERM

for t in "${TARGETS[@]}"; do
    safe="${t//./_}"
    log="logs/v2/scout_${safe}.log"
    print "→ scout @${t}  (log: ${log})"
    python -m src.v2.scout --target "$t" >> "$log" 2>&1 &
    PIDS+=($!)
done

print "→ web dashboard ${V2_WEB_URL}  (log: logs/v2/web.log)"
python -m src.v2.web --host "${V2_WEB_HOST}" --port "${V2_WEB_PORT}" >> logs/v2/web.log 2>&1 &
PIDS+=($!)

# Open the v2 dashboard (and keep Adminer one click away in the launcher log)
( sleep 3 && open "${V2_WEB_URL}" ) &

echo "------------------------------------------------------------"
print "v2 dashboard:  ${V2_WEB_URL}"
print "Adminer (DB):  ${ADMINER_URL}"
print "Tail any scout:  tail -f logs/v2/scout_<target>.log"
print "Tail web:        tail -f logs/v2/web.log"
print "Ctrl+C here to stop every v2 scout + web."
echo "------------------------------------------------------------"

# Block until all scouts exit (or until we get SIGINT and cleanup fires).
wait
