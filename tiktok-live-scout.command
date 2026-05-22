#!/usr/bin/env bash
# Double-click this file (Finder) or run it from a terminal to start the
# whole TikTok Live Scout stack: Docker → Postgres → web dashboard → scout.
#
# First run: creates the Python venv, installs deps, downloads Chromium, and
# opens a TikTok login window. Subsequent runs skip those steps.

set -uo pipefail

# Always operate from the directory this script lives in.
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
    .venv/bin/python -m playwright install chromium
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

# ---------------------------------------------------------------- Postgres
print "Starting Postgres container…"
docker compose up -d >/dev/null

until [ "$(docker inspect -f '{{.State.Health.Status}}' tiktok-live-scout-db 2>/dev/null)" = "healthy" ]; do
    sleep 1
done
print "Postgres healthy."

# ---------------------------------------------------------------- TikTok session
# Probe TikTok's homepage in a real headless browser instead of guessing from
# the cookie jar — TikTok sets `sessionid` even for anonymous visits.
print "Checking TikTok login state…"
if ! python -m src.scout --check-login >/dev/null 2>&1; then
    print "No TikTok session — opening login window."
    print "    Sign in to TikTok, wait for the For You feed, then this script continues."
    python -m src.scout --login
    if ! python -m src.scout --check-login >/dev/null 2>&1; then
        err "Still not logged in. Closing — try the login again."
        read -r -p "Press Enter to close…"; exit 1
    fi
fi
print "Logged in."

# ---------------------------------------------------------------- web + scout
mkdir -p logs

WEB_PID=""
cleanup() {
    echo
    print "Stopping web dashboard…"
    [ -n "$WEB_PID" ] && kill "$WEB_PID" 2>/dev/null || true
    wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

print "Starting web dashboard…"
python -m src.web >> logs/web.stdout.log 2>&1 &
WEB_PID=$!

# Resolve the dashboard URL from config.yaml (defaults if anything is off).
WEB_HOST=$(python -c 'from src.config import load; c=load(); print(c.web.host)' 2>/dev/null || echo "127.0.0.1")
WEB_PORT=$(python -c 'from src.config import load; c=load(); print(c.web.port)' 2>/dev/null || echo "8766")
WEB_URL="http://${WEB_HOST}:${WEB_PORT}"

( sleep 2 && open "$WEB_URL" ) &

print "Dashboard: $WEB_URL"
print "Starting scout — Ctrl+C here to stop everything."
echo "------------------------------------------------------------"
python -m src.scout
