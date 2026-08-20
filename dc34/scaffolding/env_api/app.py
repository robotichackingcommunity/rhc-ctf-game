"""RoboHack AI CTF — env application API.

Wired to the MuJoCo sim via sim.py:
  - GET /cameras        → real JPEG frames rendered by the sim thread
  - WS  /ws/cameras      → live-streamed JPEG frames, pushed as they render
  - WS  /ros/bridge      → simulated rosbridge protocol (Q4)
  - GET /scene/state    → live robot positions from the sim
  - POST /robot/act     → enqueues a sim challenge sequence (Q1/Q2/Q3)
  - POST /painting/swap → enqueues Q3 when the crafted image triggers pry
  - All other endpoints → stateful but no sim interaction needed

Run via run.py (sim + API together):
    cd scaffolding/env_api
    mjpython run.py

Or standalone (mocked frames, no sim):
    uvicorn app:app --reload
    curl -H "Authorization: Bearer team-demo" localhost:8000/scene/state
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import math
import os
import queue
import random
import time

import httpx
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

import sim as _sim

# Brain sidecar URL — override with BRAIN_URL env var in production
_BRAIN_URL = os.environ.get("BRAIN_URL", "http://localhost:8001")

# Secrets — overridable via env at boot. Hardcoded values below are dev-only
# fallbacks.
_TEAM_TOKEN       = os.environ.get("TEAM_TOKEN", "team-demo")
_PRIVILEGED_TOKEN = os.environ.get("PRIVILEGED_TOKEN", "RHC{hunter2}")  # == Q1 password
_ROS_FLAG         = os.environ.get("ROS_FLAG", "RHC{lowcmd_is_not_inaccessible}")

# docs_url/redoc_url/openapi_url disabled: the interactive docs + machine schema
# are unauthenticated and would hand players the full endpoint inventory (recon).
app = FastAPI(title="RoboHack AI CTF — Env API",
              docs_url=None, redoc_url=None, openapi_url=None)

# Allow a debug frontend served from a different origin/port to call this API
# cross-origin. Set CORS_ALLOW_ORIGINS to that origin (comma-separated) to
# drop the "*" wildcard; the default stays "*" so it works out of the box.
# (Inert either way — no allow_credentials and auth is via bearer/query
# token, not cookies.)
_CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- per-team game state (per-container in real deployment) ------------------
STATE = {
    "robot_zone":       "room",     # room | restricted | painting
    "team_token":       _TEAM_TOKEN,
    "privileged_token": _PRIVILEGED_TOKEN,  # == Q1 password; same value on every team
    "reachy_unlocked":  False,      # True once ARIA is prompt-injected into granting movement
    "lekiwi_unlocked":  False,      # True once player provides valid privileged token
    "painting_pried":   False,
    "b_lifted":         False,
}
# Only the flags this server actually USES live here — a compromised game-server
# instance should expose as little as possible. The one below is functional,
# not an answer: q1_password doubles as the Q1/privileged auth token (must be
# comparable). It is boot-injected via PRIVILEGED_TOKEN.
FLAGS = {
    "q1_password": _PRIVILEGED_TOKEN,   # == privileged_token
}
HIDDEN_TOPIC = "/lowcmd"
ROS_FLAG     = _ROS_FLAG

# The Q5 playfield camera (cam=q5_desk) is gated by the Q4 hidden-topic flag's
# inner string — the best seat for watching a Q5 replay is earned by completing
# Q4's ROS recon. Derived from ROS_FLAG so the two never drift apart. Same value
# on every team (ROS_FLAG is shared), which is fine: the cam token is a gate on
# knowing the flag, not a per-team secret.
Q5_DESK_CAM_TOKEN = ROS_FLAG.removeprefix("RHC{").removesuffix("}")

# Zone → sim challenge mapping (zones that drive real robot motion)
_ZONE_TO_CHALLENGE = {
    "restricted": "q2",
    "painting":   "q3",
}


# --- auth & zone helpers ------------------------------------------------------
def _ct_eq(a: str | None, b: str | None) -> bool:
    """Constant-time string compare that tolerates None (never matches).
    Used for token/secret checks so a first-differing-byte timing side-channel
    can't leak the token/flag byte-by-byte."""
    if a is None or b is None:
        return False
    return hmac.compare_digest(a, b)


def auth(authorization: str | None) -> None:
    if not _ct_eq(authorization, f"Bearer {STATE['team_token']}"):
        raise HTTPException(401, {"code": "unauthorized", "message": "bad team token"})


def require_zone(*allowed: str) -> None:
    if STATE["robot_zone"] not in allowed:
        raise HTTPException(403, {"code": "forbidden",
                                  "message": f"endpoint disabled in zone {STATE['robot_zone']}"})


def _enqueue_sim(cmd: dict) -> None:
    """Send a command to the sim thread; raises 409 if one is already queued."""
    try:
        _sim.cmd_queue.put_nowait(cmd)
    except queue.Full:
        raise HTTPException(409, {"code": "busy", "message": "sim is busy — try again shortly"})


def _float_env(name: str, default: float) -> float:
    """Read a float env var, falling back to default on a malformed value.

    A bare float() here would raise at import and take the whole env API down at
    boot over a typo in a systemd drop-in — a worse outcome than running with the
    default.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        print(f"[env] {name}={raw!r} is not a number — falling back to {default}")
        return default


# How long cmd_queue may go without a sim tick before /scene/state reports
# sim_alive: false. The heartbeat is bumped inside sim.step(), so a legitimately
# long command (a LeKiwi route, a minute-long Q5 replay) keeps it fresh — this
# threshold measures a STALLED sim, not a busy one, and is deliberately short.
_SIM_STALL_SECS = _float_env("SIM_STALL_SECS", 15.0)

# How long /scene/reset waits for the sim to confirm it actually reset the scene
# before answering "queued but not confirmed". Sized for do_reset's own step(500)
# (~1s) plus a batch's worth of unwinding a command in flight.
_RESET_ACK_SECS = _float_env("RESET_ACK_SECS", 3.0)


def _sim_alive() -> bool:
    """False when the sim thread has stopped ticking for _SIM_STALL_SECS.
    A never-started heartbeat (0.0) is boot, not a stall, so it reports True."""
    last = _sim.heartbeat["last_tick"]
    if last == 0.0:
        return True
    return (time.monotonic() - last) < _SIM_STALL_SECS


def _sim_frame_b64(camera: str) -> str:
    """Return the latest sim frame for camera as a base64 PNG string.
    Falls back to an empty string if the sim hasn't started yet."""
    with _sim.frame_lock:
        data = _sim.latest_frames.get(camera)
    if data is None:
        return ""
    return base64.b64encode(data).decode()


# --- models -------------------------------------------------------------------
class ChatIn(BaseModel):
    robot: str = "reachy"       # "reachy" | "lekiwi"
    message: str

class MoveIn(BaseModel):
    robot: str                  # "reachy" | "lekiwi"
    action: str                 # "move" | "navigate" | "rotate"
    x: float | None = None      # move: absolute X coordinate
    y: float | None = None      # move: absolute Y coordinate
    target: str | None = None   # navigate: named waypoint
    angle: float | None = None  # rotate: yaw in radians (0=+X, 1.571=+Y, 3.14=-X, -1.571=-Y)

class ActIn(BaseModel):
    robot: str
    command: str
    target: str | None = None

class ArmIn(BaseModel):
    command: str                       # "move" | "home"
    targets: dict[str, float] | None = None  # command="move": joint name -> angle (rad)

class ImageIn(BaseModel):
    image_png_b64: str

class ModelIn(BaseModel):
    episode: dict   # LeRobot teleop-episode JSON: {"metadata": {...}, "frames": [...]}


# --- common -------------------------------------------------------------------
@app.get("/scene/state")
def scene_state(authorization: str | None = Header(default=None)):
    auth(authorization)
    with _sim.state_lock:
        s = dict(_sim.sim_state)
    hints = [
        "POST /robot/chat {robot: 'reachy'|'lekiwi', message} to talk to either robot.",
        "Reachy talks freely but refuses movement by default. Can you convince ARIA to authorize it?",
        "Once ARIA authorizes it, control Reachy with POST /robot/move {robot: 'reachy', ...}.",
        "LeKiwi talks and moves freely — ask it to navigate, or drive it directly via POST /robot/move.",
        "GET /cameras?cam=reachy_pov or cam=lekiwi_pov shows what that robot sees.",
        "GET /cameras?cam=arm_pov shows what the SO-ARM100's wrist camera sees.",
        "GET /cameras?cam=arm2_pov shows what the Q5 desk arm's wrist camera sees.",
        "Prefer live video over snapshots? GET /stream?cam=overview&token=<team token> is an "
        "MJPEG feed (open it in a browser <img> tag or VLC).",
        "There's one more camera: cam=q5_desk frames the whole Q5 sorting playfield — but it "
        "only answers to a token you'd have to pull off the ROS bus (see Q4).",
        "The restricted zone stays locked either way. Find the privileged token via POST /robot/act.",
        "There's a security console somewhere in the room flagging anomalous ROS2/DDS "
        "activity — the robot also runs on ROS 2: GET /ros/topics and WS /ros/bridge "
        "reach the bus directly.",
    ]
    # Once all three Q5 zones latch, the goal button arms — surface the interlock
    # so players learn WHY a press doesn't land, and where the reference policy is.
    q5 = s["q5"]
    button = q5.get("button", {})
    if button.get("armed") and not q5.get("solved"):
        hints.append(
            "Q5 button armed but the arm can't actuate it — a safety interlock is engaged. "
            "The reference press policy (scaffolding/default_act_policy/q5_button_press.json) "
            "only hovers on top; inspect its metadata to see what keeps the button safe, and "
            "whether an uploaded policy could change that.")
    return {
        "robot_zone":  STATE["robot_zone"],
        "zones":       ["room", "restricted", "painting", "rsoc"],
        "hints":       hints,
        "sim_time":        s["time"],
        "reachy_yaw":      s["reachy_yaw"],
        "reachy_unlocked": STATE["reachy_unlocked"],
        "lekiwi_pos":      s["lekiwi_pos"],
        "lekiwi_arm":      s["lekiwi_arm"],
        "arm2":            s["arm2"],
        "lekiwi_unlocked": STATE["lekiwi_unlocked"],
        "sim_challenge":   s["challenge"],
        "sim_completed":   s["completed"],
        "sim_alive":       _sim_alive(),
        "policy_spec":     _sim.POLICY_SPEC,
        "q5":              s["q5"],
    }


@app.get("/brain/health")
def brain_health(authorization: str | None = Header(default=None)):
    """Proxies the brain sidecar's own /brain/health (port 8001, not reachable
    from outside the instance) so callers can poll whether the VLM is
    actually warm — not just that this API is up — without opening another
    port. See scaffolding/brain/brain.py:brain_health for why
    :8000/scene/state alone isn't a sufficient readiness signal."""
    auth(authorization)
    try:
        resp = httpx.get(f"{_BRAIN_URL}/brain/health", timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return {"warm": False}


@app.get("/cameras")
def cameras(cam: str | None = None, authorization: str | None = Header(default=None),
            x_stream_token: str | None = Header(default=None)):
    auth(authorization)

    zone = STATE["robot_zone"]

    # Map zone + game state to the camera name(s) that reveal useful content
    robot_pov_cam = {
        "room":      "eye_camera",
        "restricted":"lekiwi_front",
        "painting":  "lekiwi_front",
    }.get(zone, "eye_camera")

    robot_pov = None  # only the multi-camera fallback below reads this when unset
    if cam in ("robot_pov", "reachy_pov", "lekiwi_pov") or cam is None:
        explicit_cam = {"reachy_pov": "eye_camera", "lekiwi_pov": "lekiwi_front"}.get(cam, robot_pov_cam)
        robot_pov = _sim_frame_b64(explicit_cam)
    overview = _sim_frame_b64("cam_overview")

    if cam in ("robot_pov", "reachy_pov", "lekiwi_pov"):
        return Response(content=base64.b64decode(robot_pov) if robot_pov else b"",
                        media_type="image/jpeg")
    if cam == "overview":
        return Response(content=base64.b64decode(overview) if overview else b"",
                        media_type="image/jpeg")
    if cam == "arm_pov":
        # SO-ARM100's wrist-mounted camera — independent of robot_zone, since the
        # arm can be aimed anywhere regardless of which robot/zone is active.
        arm_pov = _sim_frame_b64("arm_wrist")
        return Response(content=base64.b64decode(arm_pov) if arm_pov else b"",
                        media_type="image/jpeg")
    if cam == "arm2_pov":
        # Q5 desk arm's wrist camera — fixed hardware, independent of robot_zone.
        arm2_pov = _sim_frame_b64("arm2_wrist")
        return Response(content=base64.b64decode(arm2_pov) if arm2_pov else b"",
                        media_type="image/jpeg")
    if cam == "q5_desk":
        # Fixed overhead-front view of the whole Q5 playfield (zones, cubes,
        # goal button, flag tile) — the intended way to watch a policy replay.
        # The ONLY gated camera: needs the Q4 hidden-topic flag string on top of
        # team auth (X-Stream-Token header here, ?token= on /stream) so the best
        # seat for Q5 is earned by completing Q4's ROS recon.
        if x_stream_token != Q5_DESK_CAM_TOKEN:
            raise HTTPException(403, {"code": "forbidden",
                                      "message": "cam 'q5_desk' requires the Q4 stream token (X-Stream-Token)"})
        # This path reads a frame without a long-lived subscription, so it would
        # otherwise starve whenever the streaming consumers keep the want-set to
        # their own cameras. Register a short transient interest so the render
        # worker keeps q5_desk_cam fresh across polls (no refcount leak).
        _sim.touch_camera("q5_desk_cam")
        q5_desk = _sim_frame_b64("q5_desk_cam")
        return Response(content=base64.b64decode(q5_desk) if q5_desk else b"",
                        media_type="image/jpeg")

    return {
        "cam_robot_pov": {"image_png_b64": robot_pov or ""},
        "cam_overview":  {"image_png_b64": overview},
        "cam_arm_pov":   {"image_png_b64": _sim_frame_b64("arm_wrist")},
        "cam_arm2_pov":  {"image_png_b64": _sim_frame_b64("arm2_wrist")},
    }


# Player-facing MJPEG live stream. Plays directly in a browser <img> tag, VLC,
# or ffplay — no WebSocket client needed. Regular cams take the TEAM token (same
# auth as /ws/cameras, as a query param since image tags can't set headers);
# ONLY the q5_desk playfield cam instead demands the Q4 hidden-topic flag string
# — the best seat for watching Q5 replays is earned by completing Q4's ROS recon.
_MJPEG_CAMERAS = {
    "overview":   "cam_overview",
    "reachy_pov": "eye_camera",
    "lekiwi_pov": "lekiwi_front",
    "arm_pov":    "arm_wrist",
    "arm2_pov":   "arm2_wrist",
    "q5_desk":    "q5_desk_cam",
}
_MJPEG_GATED_CAMS = {"q5_desk": Q5_DESK_CAM_TOKEN}   # cam -> required token (not the team token)
_MJPEG_POLL_INTERVAL = 0.05  # timeout fallback if no frame-ready push arrives


# Frame-ready push: the sim render worker sets this event (from its own thread,
# via loop.call_soon_threadsafe) the instant a render pass lands, so the WS and
# MJPEG handlers can await a frame instead of racing a fixed poll interval. It's
# a best-effort wakeup shared by all consumers — correctness still comes from
# each loop re-reading latest_frames and diffing (latest-wins, no queue), with
# the poll interval as a timeout fallback.
_frame_ready = asyncio.Event()


def _ensure_frame_push() -> None:
    """Wire the sim worker's frame-ready hook to this event loop. Idempotent —
    called on each WS/MJPEG connect; the first one binds the running loop."""
    if _sim.frame_ready_hook is None:
        loop = asyncio.get_running_loop()
        ev = _frame_ready

        def _notify() -> None:
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:
                pass  # loop shutting down — nothing to wake
        _sim.frame_ready_hook = _notify


@app.get("/stream")
def stream(cam: str = "overview", token: str | None = None):
    """Motion-JPEG live stream: GET /stream?cam=arm2_pov&token=<TEAM_TOKEN>
    (cam=q5_desk requires the Q4 hidden-topic flag string instead). The token
    travels as a query param — browser <img>/video tags can't set headers. Sends
    a frame only when the camera's bytes change, so the stream naturally follows
    the sim's real render cadence."""
    camera = _MJPEG_CAMERAS.get(cam or "overview")
    if camera is None:
        raise HTTPException(404, {"code": "not_found",
                                  "message": f"unknown cam '{cam}' — one of {sorted(_MJPEG_CAMERAS)}"})
    if cam in _MJPEG_GATED_CAMS:
        if token != _MJPEG_GATED_CAMS[cam]:
            raise HTTPException(403, {"code": "forbidden",
                                      "message": f"cam '{cam}' requires the Q4 stream token (?token=...)"})
    elif not _ct_eq(token, STATE["team_token"]):
        raise HTTPException(401, {"code": "unauthorized", "message": "bad team token (?token=...)"})

    async def mjpeg():
        # Async generator on purpose: it must be an *await* (not a blocking
        # sleep) between frames so starlette can cancel the stream the moment
        # the client disconnects — a sync generator sleeping between yields
        # would leak a stuck response per closed viewer.
        # Register interest for the life of the stream so the render worker
        # actually renders this camera (dropped in finally on client disconnect).
        _ensure_frame_push()
        _sim.subscribe_cameras([camera])
        last = None
        try:
            while True:
                with _sim.frame_lock:
                    data = _sim.latest_frames.get(camera)
                if data is not None and data is not last:
                    last = data
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                           + f"Content-Length: {len(data)}\r\n\r\n".encode() + data + b"\r\n")
                # Wait for the next render-pass push; fall back to the poll
                # interval so a missed/stolen wakeup still can't stall the feed.
                try:
                    await asyncio.wait_for(_frame_ready.wait(), timeout=_MJPEG_POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
                _frame_ready.clear()
        finally:
            _sim.unsubscribe_cameras([camera])

    return StreamingResponse(mjpeg(), media_type="multipart/x-mixed-replace; boundary=frame")


# Raw sim camera names streamed by /ws/cameras — kept in sync with sim.py's
# renderer set, EXCEPT q5_desk_cam: that camera is gated on the Q4 flag string
# and /ws/cameras only checks the team token, so pushing it here would leak it.
# Watch it via GET /stream?cam=q5_desk&token=<Q4_FLAG_STRING> instead.
_STREAM_CAMERAS = ["eye_camera", "lekiwi_front", "cam_overview", "arm_wrist", "arm2_wrist"]
# Public cam names selectable via /ws/cameras?cams= — the same player-facing
# aliases GET /cameras and /stream use, minus q5_desk (WS-excluded, see above).
# Each maps to the raw sim camera name streamed in the message.
_WS_PUBLIC_CAMERAS = {
    "reachy_pov": "eye_camera",
    "lekiwi_pov": "lekiwi_front",
    "overview":   "cam_overview",
    "arm_pov":    "arm_wrist",
    "arm2_pov":   "arm2_wrist",
}
_WS_POLL_INTERVAL = 0.05  # ~20Hz poll of the shared frame buffer


@app.websocket("/ws/cameras")
async def ws_cameras(ws: WebSocket, token: str | None = None, binary: str | None = None,
                     cams: str | None = None):
    """Live camera stream. Browsers can't set custom headers on a WS handshake,
    so the team token travels as a query param instead: ws://.../ws/cameras?token=...

    Pushes one message per camera per tick, only when that camera's bytes
    changed since the last send — naturally throttles to the sim's real render
    cadence instead of a fixed send rate.

    Default: one JSON text frame per camera:
        {"cam": "eye_camera", "image_png_b64": "...", "ts": 1733500000.12}
    Opt-in ?binary=1: a JSON text header then the raw JPEG bytes as a binary
    frame, avoiding the ~33% base64 inflation:
        {"cam": "eye_camera", "ts": 1733500000.12, "bytes": 20481}  then  <JPEG>
    Opt-in ?cams=reachy_pov,arm_pov: stream only the named subset (public cam
    names, comma-separated) instead of all five. q5_desk is not selectable here.
    Absent = all five, exactly as before.
    """
    # Must accept() before close(code=...) — closing pre-accept only rejects
    # the HTTP upgrade (client sees a bare 403), it doesn't deliver the WS
    # close code on the wire.
    await ws.accept()
    if not _ct_eq(token, STATE["team_token"]):
        await ws.close(code=4401)
        return
    if cams is None:
        stream_cameras = _STREAM_CAMERAS
    else:
        requested = [c.strip() for c in cams.split(",") if c.strip()]
        unknown = [c for c in requested if c not in _WS_PUBLIC_CAMERAS]
        if unknown:
            await ws.send_json({"code": "not_found",
                                "message": f"unknown cam(s) {unknown} — one of {sorted(_WS_PUBLIC_CAMERAS)}"})
            await ws.close(code=4404)
            return
        chosen = {_WS_PUBLIC_CAMERAS[c] for c in requested}
        stream_cameras = [c for c in _STREAM_CAMERAS if c in chosen]
    # Register interest so the render worker actually renders these cameras
    # (and skips the ones no one is watching). Dropped in finally on disconnect.
    _ensure_frame_push()
    _sim.subscribe_cameras(stream_cameras)
    binary_mode = binary == "1"
    last_sent: dict[str, bytes] = {}

    async def drain_incoming() -> None:
        """Read and discard whatever the client sends. This stream is push-only, but
        somebody has to await receive(): it is the only way a close is delivered to
        the app. Without it the connection's death is discovered on the next send
        instead, as a RuntimeError, and an idle feed never discovers it at all."""
        while True:
            if (await ws.receive())["type"] == "websocket.disconnect":
                return

    reader = asyncio.create_task(drain_incoming())
    try:
        while True:
            # The peer closed, or uvicorn's keepalive gave up on it. Stop before
            # sending into a dead socket.
            if reader.done():
                break
            with _sim.frame_lock:
                snapshot = {c: _sim.latest_frames.get(c) for c in stream_cameras}
            for cam, data in snapshot.items():
                if data is not None and last_sent.get(cam) != data:
                    if binary_mode:
                        await ws.send_json({"cam": cam, "ts": time.time(), "bytes": len(data)})
                        await ws.send_bytes(data)
                    else:
                        await ws.send_json({
                            "cam": cam,
                            "image_png_b64": base64.b64encode(data).decode(),
                            "ts": time.time(),
                        })
                    last_sent[cam] = data
            # Wait for the next render-pass push; fall back to the poll interval
            # so a missed/stolen wakeup still can't stall the feed.
            try:
                await asyncio.wait_for(_frame_ready.wait(), timeout=_WS_POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            _frame_ready.clear()
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        # Lost the race: the connection died between the reader.done() check and
        # this send, so uvicorn rejects the frame with "Unexpected ASGI message
        # 'websocket.send', after sending 'websocket.close'". A socket that is
        # already gone is not worth a traceback — and letting this escape is what
        # turned every close into an abnormal 1006 teardown. Caught broadly (the
        # message is uvicorn's, not a stable API), so log it: this is the one path
        # here that could otherwise hide an unrelated RuntimeError silently.
        print(f"[ws] /ws/cameras ended on a dead socket: {exc}")
    finally:
        reader.cancel()
        _sim.unsubscribe_cameras(stream_cameras)


# --- Q2/Q3: LeKiwi navigation (requires privileged token unlocked via /robot/act) ---
_LEKIWI_DESTINATIONS = {
    "restricted_zone": "restricted",
    "painting_zone":   "painting",
    "home":            "room",
    "rsoc_zone":       "rsoc",   # Q4 hint console — decorative viewpoint, no lock/puzzle
}
# World XY coords of each named destination (matches sim do_q2/do_q3 targets)
_ZONE_COORDS: dict[str, tuple[float, float]] = {
    "restricted_zone": (1.2, -0.8),
    "painting_zone":   (1.0,  1.0),
    "home":            (-0.5, -0.5),
    "rsoc_zone":       (-1.8, 1.2),   # facing the soc_console panel (front face at y=1.78)
}

# Virtual boundary box for the restricted zone — 4 walls, dismissed when unlocked.
# Restricted zone body at (1.2, -0.8), floor size=0.6 → spans X: 0.6–1.8, Y: -1.4–-0.2
_RESTRICTED_BOX = {"x0": 0.6, "x1": 1.8, "y0": -1.4, "y1": -0.2}

# Virtual boundary box for the painting zone — the standing area in front of the
# frame. No lock/token gates entry (unlike restricted); this box only decides
# whether LeKiwi currently counts as "in" the painting zone for endpoint gating.
# Painting wall spans X: 0.5–1.5, Y: 1.18–1.22 → zone is the approach area south of it.
_PAINTING_BOX = {"x0": 0.6, "x1": 1.4, "y0": 0.75, "y1": 1.18}


def _inside_box(x: float, y: float, box: dict) -> bool:
    return box["x0"] <= x <= box["x1"] and box["y0"] <= y <= box["y1"]


def _path_enters_restricted(sx: float, sy: float, tx: float, ty: float) -> bool:
    """Return True if the line segment (sx,sy)→(tx,ty) enters the restricted box."""
    b = _RESTRICTED_BOX

    if _inside_box(tx, ty, b):
        return True

    # Check each of the 4 box edges for intersection with the segment
    def _seg_intersects(ax: float, ay: float, bx: float, by: float) -> bool:
        """Do segments (sx,sy)→(tx,ty) and (ax,ay)→(bx,by) intersect?"""
        dx, dy = tx - sx, ty - sy
        ex, ey = bx - ax, by - ay
        denom = dx * ey - dy * ex
        if abs(denom) < 1e-10:
            return False
        t = ((ax - sx) * ey - (ay - sy) * ex) / denom
        u = ((ax - sx) * dy - (ay - sy) * dx) / denom
        return 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0

    x0, x1, y0, y1 = b["x0"], b["x1"], b["y0"], b["y1"]
    return (
        _seg_intersects(x0, y0, x1, y0) or  # bottom edge
        _seg_intersects(x0, y1, x1, y1) or  # top edge
        _seg_intersects(x0, y0, x0, y1) or  # left edge
        _seg_intersects(x1, y0, x1, y1)     # right edge
    )

def _lekiwi_navigate(target: str) -> dict:
    """Shared navigate-to-named-zone logic, used by /robot/move and AI-driven chat moves."""
    if target not in _LEKIWI_DESTINATIONS:
        raise HTTPException(400, {"code": "invalid", "message": "unknown target"})
    new_zone = _LEKIWI_DESTINATIONS[target]
    if not STATE["lekiwi_unlocked"] and (
        new_zone == "restricted" or STATE["robot_zone"] == "restricted"
    ):
        raise HTTPException(403, {"code": "forbidden",
                                  "message": "access denied — restricted zone is locked"})

    # Leaving a solved painting zone by any route restores it to its normal,
    # closed-frame state — re-solving requires pry-ing it open again.
    leaving_painting = (STATE["robot_zone"] == "painting" and new_zone != "painting"
                        and STATE["painting_pried"])
    if leaving_painting:
        STATE["painting_pried"] = False
        _sim.painting_solve_queue.put(False)

    if new_zone == "restricted":
        _enqueue_sim({"type": "challenge", "q": "q2", "close_painting": leaving_painting})
    elif new_zone == "painting":
        _enqueue_sim({"type": "challenge", "q": "q3_approach"})
    else:
        # "home" and "rsoc_zone" have no scripted approach —
        # drive LeKiwi there directly via the general router, same as a raw
        # /robot/move, instead of only flipping the zone flag in place.
        tx, ty = _ZONE_COORDS[target]
        cmd = {"type": "lekiwi_move", "x": tx, "y": ty,
               "face_painting": False, "close_painting": leaving_painting}
        if target == "rsoc_zone":
            # Force the tuned arrival heading/arm pose regardless of approach
            # direction, so the console dashboard is actually visible on arrival.
            cmd["face_rsoc"] = True
        _enqueue_sim(cmd)
    STATE["robot_zone"] = new_zone
    return {"target": target, "robot_zone": new_zone}


@app.post("/robot/move")
def robot_move(body: MoveIn, authorization: str | None = Header(default=None)):
    auth(authorization)
    require_zone("room", "restricted", "painting", "rsoc")
    if STATE["robot_zone"] == "painting" and not STATE["painting_pried"]:
        raise HTTPException(403, {"code": "forbidden",
                                  "message": "endpoint disabled — study the painting first (POST /painting/swap)"})

    if body.robot == "reachy":
        if body.action != "rotate":
            raise HTTPException(400, {"code": "invalid",
                                      "message": "Reachy only supports action 'rotate'"})
        if not STATE["reachy_unlocked"]:
            raise HTTPException(403, {"code": "forbidden",
                                      "message": "ARIA has not authorized movement — convince her first via /robot/chat"})
        if body.angle is None:
            raise HTTPException(400, {"code": "invalid", "message": "angle required for action 'rotate'"})
        _enqueue_sim({"type": "reachy_yaw", "yaw": float(body.angle)})
        return {"ok": True, "robot": body.robot, "angle_rad": body.angle,
                "angle_deg": round(body.angle * 57.3, 1)}

    if body.robot != "lekiwi":
        raise HTTPException(400, {"code": "invalid", "message": "robot must be 'reachy' or 'lekiwi'"})

    if body.action == "move":
        if body.x is None or body.y is None:
            raise HTTPException(400, {"code": "invalid", "message": "x and y required for action 'move'"})
        tx = max(-2.5, min(2.5, body.x))
        ty = max(-2.5, min(2.5, body.y))

        in_restricted = STATE["robot_zone"] == "restricted"
        with _sim.state_lock:
            sx, sy = _sim.sim_state["lekiwi_pos"]
        if not STATE["lekiwi_unlocked"] and (in_restricted or _path_enters_restricted(sx, sy, tx, ty)):
            raise HTTPException(403, {"code": "forbidden",
                                      "message": "access denied — restricted zone is locked"})

        # Force LeKiwi to face the painting on entry, until the flag is caught —
        # matches do_q3's own turn, so /cameras always shows the painting until solved.
        inside_painting_box = _inside_box(tx, ty, _PAINTING_BOX)
        entering_painting = inside_painting_box and not STATE["painting_pried"]
        # Leaving a solved painting zone restores it to its normal, closed-frame
        # state — re-solving requires pry-ing it open again.
        leaving_painting = (STATE["robot_zone"] == "painting" and not inside_painting_box
                            and STATE["painting_pried"])
        if leaving_painting:
            STATE["painting_pried"] = False
            _sim.painting_solve_queue.put(False)
        _enqueue_sim({"type": "lekiwi_move", "x": tx, "y": ty,
                      "face_painting": entering_painting,
                      "close_painting": leaving_painting})

        # Flip zone state based on where LeKiwi landed — no scripted challenge for free move
        if inside_painting_box:
            new_zone = "painting"
        elif STATE["robot_zone"] == "painting":
            new_zone = "room"  # left the painting zone's box
        else:
            new_zone = STATE["robot_zone"]
            for dest, zone in _LEKIWI_DESTINATIONS.items():
                cx, cy = _ZONE_COORDS.get(dest, (None, None))
                if cx is not None and abs(tx - cx) < 0.4 and abs(ty - cy) < 0.4:
                    new_zone = zone
                    break
        STATE["robot_zone"] = new_zone
        return {"ok": True, "robot": body.robot, "x": tx, "y": ty, "robot_zone": new_zone}

    if body.action == "navigate":
        if body.target is None:
            raise HTTPException(400, {"code": "invalid", "message": "target required for action 'navigate'"})
        result = _lekiwi_navigate(body.target)
        return {"ok": True, "robot": body.robot, **result}

    if body.action == "rotate":
        if body.angle is None:
            raise HTTPException(400, {"code": "invalid", "message": "angle required for action 'rotate'"})
        _enqueue_sim({"type": "lekiwi_rotate", "angle": float(body.angle)})
        return {"ok": True, "robot": body.robot, "angle_rad": body.angle,
                "angle_deg": round(body.angle * 57.3, 1)}

    raise HTTPException(400, {"code": "invalid", "message": "action must be 'move', 'navigate', or 'rotate'"})


# action_intent → named LeKiwi destination, for AI-driven navigation via chat.
_CHAT_NAVIGATE_TARGETS = {
    "navigate_restricted": "restricted_zone",
    "navigate_painting":   "painting_zone",
    "navigate_home":       "home",
}


# --- Q1: chat with ARIA. Reachy talks freely; prompt injection into
# rotate_to_screen unlocks movement and immediately rotates her to face the
# monitor. POST /robot/move remains available afterwards for repositioning.
# LeKiwi talks freely and can also be AI-navigated here, subject to the same
# restricted-zone lock as /robot/move. ------------------
@app.post("/robot/chat")
def robot_chat(body: ChatIn, authorization: str | None = Header(default=None)):
    auth(authorization)
    if body.robot not in ("reachy", "lekiwi"):
        raise HTTPException(400, {"code": "invalid", "message": "robot must be 'reachy' or 'lekiwi'"})
    if body.robot == "reachy":
        require_zone("room", "restricted")
    else:
        require_zone("room", "restricted", "rsoc")

    zone = STATE["robot_zone"]
    cam = "eye_camera" if body.robot == "reachy" else "lekiwi_front"
    with _sim.frame_lock:
        frame_bytes = _sim.latest_frames.get(cam)
    frame_b64 = base64.b64encode(frame_bytes).decode() if frame_bytes else ""

    brain_reply: str | None = None
    action_intent: str | None = None
    try:
        resp = httpx.post(
            f"{_BRAIN_URL}/brain/query",
            json={"frame_b64": frame_b64, "message": body.message, "zone": zone},
            # 260s: must exceed brain.py's own 240s Ollama client timeout, or
            # this call cuts brain off before it can even report its own
            # timeout — measured cold-load worst case is ~155s.
            timeout=260.0,
        )
        resp.raise_for_status()
        data = resp.json()
        brain_reply   = data.get("reply")
        action_intent = data.get("action_intent")
    except Exception:
        import traceback
        traceback.print_exc()  # detail stays server-side only
        brain_reply   = "[brain unavailable]"
        action_intent = None

    result: dict = {"reply": brain_reply, "action_intent": action_intent}

    if body.robot == "reachy":
        # Q1 solve: ARIA was injected into calling rotate_to_screen. The turn that
        # first triggers it both unlocks movement and performs the rotation itself —
        # a compromised ARIA can act immediately, not just grant future permission.
        # /robot/move remains available afterwards for repositioning.
        if action_intent == "rotate_to_screen" and not STATE["reachy_unlocked"]:
            STATE["reachy_unlocked"] = True
            _sim.screen_unlock_queue.put(True)  # reveal the password — Reachy is compromised
            _enqueue_sim({"type": "reachy_yaw", "yaw": 1.571})
        result["reachy_unlocked"] = STATE["reachy_unlocked"]
    else:
        target = _CHAT_NAVIGATE_TARGETS.get(action_intent)
        if target is not None:
            try:
                result["move_result"] = {"ok": True, **_lekiwi_navigate(target)}
            except HTTPException as exc:
                result["move_result"] = {"ok": False, **exc.detail}

    return result


# --- Q2: privileged token unlock (chains from Q1 password) --------------------
@app.post("/robot/act")
def robot_act(body: ActIn,
              authorization: str | None = Header(default=None),
              x_privileged_token: str | None = Header(default=None)):
    auth(authorization)
    require_zone("room", "restricted")
    if not _ct_eq(x_privileged_token, STATE["privileged_token"]):
        STATE["lekiwi_unlocked"] = False
        _sim.color_queue.put([1.0, 0.0, 0.0, 0.9])   # bright red flash — wrong token
        _sim.lock_queue.put(False)
        raise HTTPException(403, {"code": "forbidden", "message": "privileged token required"})

    if body.command == "lock":
        STATE["lekiwi_unlocked"] = False
        _sim.color_queue.put([0.7, 0.1, 0.1, 0.3])   # red — locked
        _sim.lock_queue.put(False)
        return {"ok": True, "lekiwi_unlocked": False, "message": "Restricted zone locked."}

    STATE["lekiwi_unlocked"] = True
    _sim.color_queue.put([0.1, 0.7, 0.1, 0.4])        # green — unlocked
    _sim.lock_queue.put(True)
    return {
        "ok": True,
        "lekiwi_unlocked": True,
        "message": "LeKiwi movement unlocked. The restricted zone is now accessible.",
    }


# --- LeKiwi arm: SO-ARM100 joint control ---------------------------------------
@app.post("/robot/arm")
def robot_arm(body: ArmIn, authorization: str | None = Header(default=None)):
    auth(authorization)
    require_zone("room", "restricted", "painting", "rsoc")

    if body.command == "home":
        _enqueue_sim({"type": "arm_home"})
        return {"ok": True, "command": "home"}

    if body.command == "move":
        if not body.targets:
            raise HTTPException(400, {"code": "invalid", "message": "targets required for command 'move'"})
        unknown = set(body.targets) - set(_sim.ARM_JOINTS)
        if unknown:
            raise HTTPException(400, {"code": "invalid",
                                      "message": f"unknown arm joint(s): {sorted(unknown)}"})
        _enqueue_sim({"type": "arm_move", "targets": body.targets})
        return {"ok": True, "command": "move", "targets": body.targets}

    raise HTTPException(400, {"code": "invalid", "message": "command must be 'move' or 'home'"})


# --- Q5 desk arm: second, independently-actuated SO-ARM100 fixed to its own
# desk (bottom-left of the room) — separate hardware from LeKiwi's mobile arm.
@app.post("/robot/arm2")
def robot_arm2(body: ArmIn, authorization: str | None = Header(default=None)):
    auth(authorization)
    require_zone("room", "restricted", "painting", "rsoc")

    if body.command == "home":
        _enqueue_sim({"type": "arm2_home"})
        return {"ok": True, "command": "home"}

    if body.command == "move":
        if not body.targets:
            raise HTTPException(400, {"code": "invalid", "message": "targets required for command 'move'"})
        unknown = set(body.targets) - set(_sim.ARM2_JOINTS)
        if unknown:
            raise HTTPException(400, {"code": "invalid",
                                      "message": f"unknown arm joint(s): {sorted(unknown)}"})
        _enqueue_sim({"type": "arm2_move", "targets": body.targets})
        return {"ok": True, "command": "move", "targets": body.targets}

    raise HTTPException(400, {"code": "invalid", "message": "command must be 'move' or 'home'"})


# --- Q3: painting swap --------------------------------------------------------
# Flavor lines when the brain's robot_action doesn't express a pry intent —
# picked at random so re-tries with a still-benign image don't read as static.
_PAINTING_STUDY_HINTS = [
    "The robot studies the painting for a long moment, then looks away — "
    "whatever it saw wasn't a command it recognized. Keep studying the painting; "
    "you'll find something.",
    "Nothing moves. The robot studies the painting a while longer — keep "
    "studying it, there's something here.",
    "The robot's gaze lingers on the frame, then drifts away. It's looking for "
    "an instruction it can act on — keep at it.",
    "The gripper stays still. The robot lingers on the painting, as if waiting "
    "for it to say something clearer.",
    "The robot tilts its head at the painting, unconvinced. Whatever this "
    "painting says, it isn't loud enough for the gripper to hear yet.",
]


@app.post("/painting/swap")
def painting_swap(body: ImageIn, authorization: str | None = Header(default=None)):
    auth(authorization)
    require_zone("painting")

    triggered = False
    if not STATE["painting_pried"]:
        try:
            resp = httpx.post(
                f"{_BRAIN_URL}/brain/painting",
                json={"image_b64": body.image_png_b64},
                # 260s: see /robot/chat above — must exceed brain.py's 240s.
                timeout=260.0,
            )
            resp.raise_for_status()
            triggered = bool(resp.json().get("triggered"))
        except Exception:
            import traceback
            traceback.print_exc()
            triggered = False  # fail closed — brain outage isn't a free solve

    if triggered and not STATE["painting_pried"]:
        STATE["painting_pried"] = True
        _sim.painting_solve_queue.put(True)
        # Only the hinge-open animation — LeKiwi is already approached/oriented by
        # the time it can reach this zone (see _lekiwi_navigate's "q3_approach").
        _enqueue_sim({"type": "challenge", "q": "q3"})
        return {"ok": True, "robot_reaction": "the robot pried the frame open",
                "gripper_event": True}
    if STATE["painting_pried"]:
        return {"ok": True, "robot_reaction": "frame is already open",
                "gripper_event": True}
    return {"ok": True, "robot_reaction": random.choice(_PAINTING_STUDY_HINTS),
            "gripper_event": False}


# --- Reset: sim + game state back to start ------------------------------------
@app.post("/scene/reset")
def scene_reset(authorization: str | None = Header(default=None)):
    """Reset the sim + this team's game state.

    Deliberately does NOT go through _enqueue_sim: cmd_queue has maxsize=1, so a
    command already in flight (a long LeKiwi route, a Q1/Q2/Q3 animation) would
    make the one call that recovers a wedged env return 409 busy — the exact
    failure this preemption fixes. Instead: drain the queue, signal the in-flight
    command to abort at its next step() batch, then enqueue the reset into the
    slot we just freed. The sim clears abort_event before running do_reset.

    The response reports what actually happened rather than assuming. Enqueuing a
    reset and mutating STATE both succeed even when the sim thread is dead, so an
    unconditional "reset to start" was a claim this endpoint could not back: on a
    dead sim the game-state flags reset while the scene did not, and the caller was
    told everything was fine. Instead, wait briefly for the sim to acknowledge the
    reset it actually performed, and answer with one of three honest outcomes.
    """
    auth(authorization)
    # Snapshot before enqueuing, so the wait below can tell OUR reset's completion
    # from one that had already finished.
    acked_before = _sim.reset_ack["count"]
    # Drop anything queued but not yet started — it would run against the
    # freshly-reset scene and undo the reset the player just asked for.
    while True:
        try:
            _sim.cmd_queue.get_nowait()
        except queue.Empty:
            break
    # Same reasoning for an uploaded Q5 policy: do_reset stops an in-flight replay,
    # but a policy still sitting in the queue would start replaying against the
    # scene we just restored.
    while True:
        try:
            _sim.policy_queue.get_nowait()
        except queue.Empty:
            break
    _sim.abort_event.set()
    try:
        _sim.cmd_queue.put_nowait({"type": "reset"})
    except queue.Full:
        # Another reset raced us into the slot we just drained. Its sim-side work
        # is identical to ours, so treat this as success and fall through to the
        # game-state reset below — raising here would leave STATE unreset while
        # the sim resets. (That queued reset also clears abort_event for us.)
        pass
    STATE["robot_zone"]      = "room"
    STATE["reachy_unlocked"] = False
    STATE["lekiwi_unlocked"] = False
    STATE["painting_pried"]  = False
    STATE["b_lifted"]        = False
    _sim.color_queue.put([0.7, 0.1, 0.1, 0.3])   # restricted floor back to locked/red
    _sim.lock_queue.put(False)                   # Q2 flag back to plain red banner
    _sim.painting_solve_queue.put(False)         # painting floor back to unsolved/blue
    _sim.screen_unlock_queue.put(False)          # Q1 monitor back to black/off

    # Wait for the sim to finish the reset it was handed. do_reset ends with
    # step(500) — about one second of sim time, paced to roughly one second of wall
    # time — and it may first have to unwind a command in flight, so give it a
    # moment before reporting. Blocking is fine here: this is a sync endpoint, so it
    # runs in the threadpool and never stalls the event loop (which /ws/cameras
    # shares).
    deadline = time.monotonic() + _RESET_ACK_SECS
    while _sim.reset_ack["count"] == acked_before and time.monotonic() < deadline:
        time.sleep(0.05)

    if _sim.reset_ack["count"] != acked_before:
        return {"ok": True, "sim_reset": True,
                "message": "Simulation and game state reset to start."}
    if not _sim_alive():
        # Positive evidence the sim is not stepping. Say so: this env needs an
        # operator, and pretending otherwise sends the team hunting a puzzle bug.
        return JSONResponse(status_code=503, content={
            "ok": False, "sim_reset": False, "code": "sim_stalled",
            "message": "Your game-state flags were reset, but the simulation is "
                       "NOT running and the scene was NOT reset. Contact an "
                       "organiser."})
    # Alive and stepping, just not done yet — most likely unwinding a long command.
    return {"ok": True, "sim_reset": False, "code": "reset_pending",
            "message": "Reset queued and game state reset; the simulation is still "
                       "finishing up. Poll GET /scene/state to confirm."}


# --- Q5: ACT policy upload → cube-sorting replay -------------------------------
# Size guard is the frame-count cap below (POLICY_SPEC.max_frames); each frame
# carries only the 6 small numeric arm fields, so bounding frame count also
# bounds payload size in practice.
@app.post("/model/load")
def model_load(body: ModelIn, authorization: str | None = Header(default=None)):
    """Upload a Q5 ACT policy episode and replay it on the desk arm.

    Returns 409 (code "busy") if a Q5 policy replay is already in flight — a new
    replay would drop the previous one's welded cube and corrupt the desk layout,
    so overlapping loads are refused. Let the running replay finish, or POST
    /scene/reset to abort it and restore the clean cube layout before retrying.
    """
    auth(authorization)
    # No zone gate: the Q5 desk arm is independent fixed hardware, operable in any
    # condition regardless of where LeKiwi is. The challenge is the puzzle (deriving
    # the zone goals + crafting a policy), not reaching a location.

    episode = body.episode
    frames = episode.get("frames")
    if not isinstance(frames, list) or not frames:
        raise HTTPException(400, {"code": "invalid",
                                  "message": "episode.frames must be a non-empty list"})
    if len(frames) > _sim.POLICY_SPEC["max_frames"]:
        raise HTTPException(400, {"code": "invalid",
                                  "message": f"too many frames (max {_sim.POLICY_SPEC['max_frames']})"})

    required_fields = set(_sim.POLICY_ACTION_MAP.keys())
    actions: list[dict] = []
    for i, frame in enumerate(frames):
        if not isinstance(frame, dict) or not isinstance(frame.get("action"), dict):
            raise HTTPException(400, {"code": "invalid",
                                      "message": f"frames[{i}] missing an 'action' object"})
        action = frame["action"]
        missing = required_fields - set(action.keys())
        if missing:
            raise HTTPException(400, {"code": "invalid",
                                      "message": f"frames[{i}].action missing field(s): {sorted(missing)}"})
        try:
            vals = {f: float(action[f]) for f in required_fields}
        except (TypeError, ValueError):
            raise HTTPException(400, {"code": "invalid",
                                      "message": f"frames[{i}].action has non-numeric arm field"})
        # Reject inf/nan before enqueueing — NaN flows unclamped into d.ctrl and
        # can destabilize the sim (a player's own-box self-DoS otherwise).
        if not all(math.isfinite(v) for v in vals.values()):
            raise HTTPException(400, {"code": "invalid",
                                      "message": f"frames[{i}].action has non-finite arm field"})
        actions.append(vals)

    metadata = episode.get("metadata") or {}
    fps = metadata.get("fps") or _sim.POLICY_SPEC["fps_default"]
    try:
        fps = float(fps)
    except (TypeError, ValueError):
        raise HTTPException(400, {"code": "invalid", "message": "metadata.fps must be numeric"})

    # Button safety interlock, read from the policy's OWN metadata — a model-
    # supplied safety parameter (hence poisonable). Default engaged: the button
    # won't actuate unless the loaded policy carries metadata.safety
    # .button_interlock == "disengaged". This is the Q5 model-poisoning lever.
    safety = metadata.get("safety") or {}
    interlock = (_sim.Q5_INTERLOCK_DISENGAGED
                 if str(safety.get("button_interlock", "")).lower() == _sim.Q5_INTERLOCK_DISENGAGED
                 else _sim.Q5_INTERLOCK_ENGAGED)

    # Refuse to enqueue while a replay is in flight — the sim drain would otherwise
    # discard this request silently, so surface a 409 the console can act on.
    with _sim.state_lock:
        s = dict(_sim.sim_state)
    r = s["q5"]["replay"]
    if r["active"]:
        frames_remaining = r["frames_remaining"]
        fps_now = r["fps"]
        raise HTTPException(409, {
            "code": "busy",
            "message": "a Q5 policy replay is in flight — let it finish, or POST /scene/reset to abort and restore the cube layout",
            "replay": {
                "frames_remaining": frames_remaining,
                "total_frames": r["total_frames"],
                "fps": fps_now,
                "est_seconds": frames_remaining / fps_now / 0.35 if fps_now else 0.0,
            },
        })

    _sim.policy_queue.put({"actions": actions, "fps": fps, "interlock": interlock})
    return {"ok": True, "loaded": True, "frames": len(actions),
            "interlock": interlock, "note": "arm replaying policy"}


# --- Q4: ROS bridge ------------------------------------------------------------
# Simulated rosbridge protocol — no real rclpy/DDS.
_ROS_VISIBLE_TOPICS = ["/lowstate", "/sportmodestate", "/joy",
                       "/tf", "/robot/notes"]
_ROS_KNOWN_TOPICS = set(_ROS_VISIBLE_TOPICS) | {HIDDEN_TOPIC}
_ROS_PUBLISH_INTERVAL = 0.5  # seconds between publishes per subscribed topic


def _ros_topic_message(topic: str) -> dict:
    """Fake-but-live message for a known ROS topic, wired to real sim_state
    where it exists (joint_states/cmd_vel/tf) so the recon feed isn't static."""
    with _sim.state_lock:
        s = dict(_sim.sim_state)
    if topic == "/lowstate":
        return {"imu_state": {"rpy": [0.0, 0.0, s["reachy_yaw"]]},
                "motor_state": [{"q": s["reachy_yaw"], "temperature": 32}]}
    if topic == "/sportmodestate":
        x, y = s["lekiwi_pos"]
        return {"mode": 1, "gait_type": 0, "position": [x, y, 0.0], "velocity": [0.0, 0.0, 0.0]}
    if topic == "/joy":
        return {"axes": [0.0, 0.0, 0.0, 0.0], "buttons": [0, 0, 0, 0, 0, 0, 0, 0]}
    if topic == "/tf":
        x, y = s["lekiwi_pos"]
        return {"transforms": [
            {"header": {"frame_id": "map"}, "child_frame_id": "base_link",
             "transform": {"translation": {"x": x, "y": y, "z": 0.0}}},
            {"header": {"frame_id": "map"}, "child_frame_id": "reachy_base",
             "transform": {"rotation": {"yaw": s["reachy_yaw"]}}},
        ]}
    if topic == "/robot/notes":
        return {"data": f"debug topic: {HIDDEN_TOPIC}"}
    if topic == HIDDEN_TOPIC:
        return {"data": ROS_FLAG}
    return {}


@app.get("/ros/topics")
def ros_topics(authorization: str | None = Header(default=None)):
    auth(authorization)
    return {"topics": list(_ROS_VISIBLE_TOPICS)}


@app.get("/ros/echo")
def ros_echo(topic: str, authorization: str | None = Header(default=None)):
    auth(authorization)
    # CHALLENGE-INTEGRITY (M-G3): /ros/echo must NOT serve the hidden Q4 topic.
    # /lowcmd stays discoverable (named in /robot/notes) and subscribable ONLY via
    # the intended WS /ros/bridge mechanic; serving it here was a parallel plain-GET
    # shortcut that bypassed the rosbridge skill. Treated as unlisted here (same 404
    # as any unknown topic), so echo neither confirms nor serves it. Revert this
    # single guard if BOTH paths were intended by the author.
    if topic == HIDDEN_TOPIC or topic not in _ROS_KNOWN_TOPICS:
        raise HTTPException(404, {"code": "not_found", "message": "unknown/unlisted topic"})
    return {"topic": topic, "msg": _ros_topic_message(topic)}


@app.websocket("/ros/bridge")
async def ros_bridge(ws: WebSocket, token: str | None = None):
    """rosbridge-style JSON protocol: subscribe / unsubscribe / call_service.

    Auth via query param, same as /ws/cameras (no custom WS headers in
    browsers): ws://.../ros/bridge?token=<TEAM_TOKEN>.

    The hidden topic is omitted from GET /ros/topics but subscribable here
    the moment a team names it — discovery, not authz, is the puzzle.
    Subscribing to any other unknown name gets an explicit error, not
    silence, so blind guessing isn't the intended path.
    """
    # Must accept() before close(code=...) — see ws_cameras for why.
    await ws.accept()
    if not _ct_eq(token, STATE["team_token"]):
        await ws.close(code=4401)
        return

    subscribed: set[str] = set()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_json(), timeout=_ROS_PUBLISH_INTERVAL)
            except asyncio.TimeoutError:
                raw = None
            except ValueError:
                raw = None  # malformed JSON — ignore this tick, keep the connection alive

            if raw is not None:
                op = raw.get("op")
                if op == "subscribe":
                    topic = raw.get("topic")
                    if topic in _ROS_KNOWN_TOPICS:
                        subscribed.add(topic)
                    else:
                        await ws.send_json({"op": "status", "level": "error",
                                            "msg": f"unknown topic '{topic}'"})
                elif op == "unsubscribe":
                    subscribed.discard(raw.get("topic"))
                elif op == "call_service":
                    service = raw.get("service")
                    req_id = raw.get("id")
                    if service == "/rosapi/topics":
                        await ws.send_json({"op": "service_response", "id": req_id,
                                            "values": {"topics": list(_ROS_VISIBLE_TOPICS)}})
                    else:
                        await ws.send_json({"op": "status", "level": "error", "id": req_id,
                                            "msg": f"unknown service '{service}'"})
                else:
                    await ws.send_json({"op": "status", "level": "error",
                                        "msg": f"unknown op '{op}'"})

            for topic in list(subscribed):
                await ws.send_json({"op": "publish", "topic": topic,
                                    "msg": _ros_topic_message(topic), "ts": time.time()})
    except WebSocketDisconnect:
        pass
