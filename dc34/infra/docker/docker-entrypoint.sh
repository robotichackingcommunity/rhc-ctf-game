#!/usr/bin/env bash
# Starts Xvfb as a background sibling process, waits for it to be up, then
# execs `python run.py` as the container's main process (PID 1 hands off via
# exec so signals reach it directly).
#
# This image does not bundle a model server — BRAIN_BACKEND/BRAIN_BASE_URL/
# BRAIN_MODEL must point at a player-supplied OpenAI-compatible endpoint
# (local Ollama, vLLM, hosted API, etc.). See README.md.
set -euo pipefail

if [ -z "${BRAIN_BASE_URL:-}" ]; then
    echo "ERROR: BRAIN_BASE_URL is not set." >&2
    echo "Point it at an OpenAI-compatible inference endpoint (local Ollama, vLLM, a hosted API, ...)." >&2
    echo "See README.md for example configs." >&2
    exit 1
fi

# Stale lock from a prior run in this same container (e.g. `docker start`
# after a kill/crash reuses the writable layer) makes Xvfb refuse to bind
# :99 with "Server is already active for display 99" — clear it first.
DISPLAY_NUM="${DISPLAY#:}"
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

Xvfb "$DISPLAY" -screen 0 1280x800x24 &
XVFB_PID=$!

cleanup() {
    kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup TERM INT

echo "Waiting for Xvfb on ${DISPLAY}..."
for i in $(seq 1 30); do
    if [ -e "/tmp/.X11-unix/X${DISPLAY_NUM}" ]; then
        echo "Xvfb is up."
        break
    fi
    sleep 0.5
done

cd /opt/ctf/scaffolding/env_api
exec /opt/ctf/venv/bin/python3 run.py
