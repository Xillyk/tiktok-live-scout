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

# ---------------------------------------------------------------- pick profile
print "Choosing scout profile…"
PROFILE=$(python -m src.pick_profile) || {
    err "Profile selection cancelled. Closing."
    exit 0
}
print "Profile: ${PROFILE}"

# ---------------------------------------------------------------- per-profile cleanup
# Kill only the previous instance OF THIS PROFILE — so a second launcher
# window can scout a different profile in parallel without trampling on it.
PROFILE_USER_DIR=$(python -c "
import sys
from pathlib import Path
from src.config import load
try:
    print(Path(load().profile('${PROFILE}').user_data_dir).resolve())
except KeyError as e:
    print(e, file=sys.stderr)
    sys.exit(1)
")
print "Cleaning up any stale '${PROFILE}' instance..."
pkill -f "src\\.scout .*--profile ${PROFILE}\\b"     2>/dev/null
pkill -f "src\\.scout .*--profile=${PROFILE}\\b"     2>/dev/null
pkill -f "user-data-dir=${PROFILE_USER_DIR}"         2>/dev/null
sleep 1
pkill -9 -f "user-data-dir=${PROFILE_USER_DIR}"      2>/dev/null
true

# ---------------------------------------------------------------- TikTok session
# Probe TikTok's homepage to check the selected profile's session — TikTok
# sets sessionid even for anonymous visits, so the cookie jar alone isn't
# trustworthy.
print "Checking TikTok login state for ${PROFILE}..."
if ! python -m src.scout --check-login --profile "${PROFILE}" >/dev/null 2>&1; then
    print "No TikTok session for ${PROFILE} — opening login window."
    print "    Sign in to TikTok, wait for the For You feed, then this script continues."
    python -m src.scout --login --profile "${PROFILE}"
    if ! python -m src.scout --check-login --profile "${PROFILE}" >/dev/null 2>&1; then
        err "Still not logged in. Closing — try the login again."
        read -r -p "Press Enter to close..."; exit 1
    fi
fi
print "Logged in as ${PROFILE}."

# ---------------------------------------------------------------- web + scout
mkdir -p logs

# Resolve the dashboard URL from config.yaml (defaults if anything is off).
WEB_HOST=$(python -c 'from src.config import load; c=load(); print(c.web.host)' 2>/dev/null || echo "127.0.0.1")
WEB_PORT=$(python -c 'from src.config import load; c=load(); print(c.web.port)' 2>/dev/null || echo "8766")
WEB_URL="http://${WEB_HOST}:${WEB_PORT}"

# Reuse the existing web dashboard if another launcher window already started
# one (multi-profile parallel runs share the same dashboard).
WEB_PID=""
if curl -fsS --max-time 2 "${WEB_URL}/api/state" >/dev/null 2>&1; then
    print "Web dashboard already running on ${WEB_URL} — reusing it."
else
    print "Starting web dashboard at ${WEB_URL}..."
    python -m src.web >> logs/web.stdout.log 2>&1 &
    WEB_PID=$!
fi

cleanup() {
    echo
    if [ -z "${WEB_PID}" ]; then
        print "Shared web dashboard left running for the other launcher."
        return
    fi
    # We started the web. Only tear it down if no OTHER scout is still alive
    # (so the surviving launcher's dashboard stays usable).
    if pgrep -f 'src\.scout .*--profile ' >/dev/null 2>&1; then
        print "Another profile is still scouting — leaving web up."
    else
        print "Stopping web dashboard (no other scouts running)..."
        kill "${WEB_PID}" 2>/dev/null || true
        wait 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

( sleep 2 && open "${WEB_URL}" ) &

print "Dashboard: ${WEB_URL}"
print "Starting scout (${PROFILE}) — Ctrl+C here to stop just this profile."
echo "------------------------------------------------------------"
python -m src.scout --profile "${PROFILE}"
