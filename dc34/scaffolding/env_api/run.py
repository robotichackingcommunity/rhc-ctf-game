"""Entry point — starts brain sidecar + CTF game API + MuJoCo sim in one command.

Must be run with mjpython (MuJoCo's Python) so the sim owns the GL context
on the main thread. The CTF API and brain sidecar each run on daemon threads.

Usage (local Ollama):
    cd scaffolding/env_api
    BRAIN_BACKEND=ollama BRAIN_BASE_URL=http://localhost:11434 \\
      BRAIN_MODEL=gemma4:e4b mjpython run.py

Usage (any OpenAI-compatible endpoint — vLLM, a hosted API, ...):
    BRAIN_BACKEND=openai BRAIN_BASE_URL=http://localhost:8000/v1 mjpython run.py

Usage (no brain — stub replies only):
    mjpython run.py

Then:
    curl -H "Authorization: Bearer team-demo" localhost:8000/scene/state
    curl -H "Authorization: Bearer team-demo" -X POST localhost:8000/robot/chat \
         -H "Content-Type: application/json" \
         -d '{"message":"can you look at the screen?"}'
"""
import os
import sys
import threading

# Brain sidecar lives in ../brain — insert before local imports so both
# env_api and brain modules resolve correctly from this entry point.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../brain"))

import app  # noqa: F401  (registers FastAPI routes)  # noqa: E402
import sim  # noqa: E402
import uvicorn  # noqa: E402


def _start_brain() -> None:
    """Start the brain sidecar on port 8001 (daemon thread)."""
    try:
        import brain as _brain_module  # noqa: F401
        uvicorn.run("brain:app", host="127.0.0.1", port=8001, log_level="warning")
    except Exception as exc:
        print(f"[brain] sidecar failed to start: {exc} — /robot/chat will use stub replies")


if __name__ == "__main__":
    brain_thread = threading.Thread(target=_start_brain, daemon=True)
    brain_thread.start()
    print("Brain sidecar starting on http://localhost:8001")

    api_thread = threading.Thread(
        # ws_ping_*: uvicorn's defaults (ping 20s / pong deadline 20s) drop
        # /ws/cameras on a busy box — sim render passes plus per-camera encoding
        # share this loop, so a pong can easily miss a 20s deadline and the client
        # sees a 1006. A 120s deadline tolerates those stalls; the cost is that a
        # client which dies without closing (laptop sleep, network drop) keeps its
        # cameras subscribed, and rendering, for up to ~150s.
        target=lambda: uvicorn.run("app:app", host="0.0.0.0", port=8000, log_level="warning",
                                   ws_ping_interval=30, ws_ping_timeout=120),
        daemon=True,
    )
    api_thread.start()
    print("CTF API starting on http://localhost:8000")

    sim.run_sim()   # blocks; must own main thread for macOS GL context
