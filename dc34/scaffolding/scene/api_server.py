"""
CTF API Server — REST interface for the MuJoCo CTF simulation.

Architecture:
  Main thread  : MuJoCo sim loop (mj_step + viewer.sync). Must own the GL context.
  API thread   : FastAPI/uvicorn daemon. Reads shared frame buffers, posts commands.
  Shared state : thread-safe via locks + a command queue.

Endpoints:
  GET  /                        → HTML status page
  GET  /cameras                 → list available cameras
  GET  /camera/{name}           → latest JPEG frame from that camera
  GET  /state                   → current sim state (positions, time, challenge status)
  POST /challenge/{q}           → queue challenge Q1, Q2, or Q3
  POST /reset                   → queue full reset

Run from ctf_scene/:
    mjpython api_server.py

Then in another terminal:
    curl http://localhost:8000/state
    curl http://localhost:8000/camera/cam_overview --output frame.jpg
    curl -X POST http://localhost:8000/challenge/q1
"""
import io
import math
import os
import threading
import time
import queue
from typing import Optional

import mujoco
import mujoco.viewer
import numpy as np
from PIL import Image

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response

# ── Shared state ──────────────────────────────────────────────────────────────
# Frames: camera_name -> JPEG bytes. Written by sim thread, read by API thread.
frame_lock   = threading.Lock()
latest_frames: dict[str, bytes] = {}

# Command queue: sim thread consumes one command per cycle.
cmd_queue: queue.Queue = queue.Queue(maxsize=1)

# Sim state snapshot: written by sim thread.
state_lock = threading.Lock()
sim_state: dict = {
    "time":        0.0,
    "reachy_yaw":  0.0,
    "lekiwi_pos":  [0.0, 0.0],
    "challenge":   None,   # current challenge ("q1"/"q2"/"q3"/None)
    "completed":   [],
}

# ── FastAPI app ───────────────────────────────────────────────────────────────
app = FastAPI(title="CTF Robot API", version="1.0")

CAMERA_NAMES = ["eye_camera", "lekiwi_front", "cam_overview"]

@app.get("/", response_class=HTMLResponse)
def index():
    with state_lock:
        s = dict(sim_state)
    cameras_html = "".join(
        f'<li><a href="/camera/{c}">{c}</a></li>' for c in CAMERA_NAMES
    )
    completed = ", ".join(s["completed"]) or "none"
    return f"""
    <html><head><title>CTF Robot API</title>
    <style>body{{font-family:monospace;background:#111;color:#0f0;padding:2em}}
    a{{color:#0ff}} h1{{color:#ff0}}</style></head>
    <body>
    <h1>RoboHack AI CTF — Robot API</h1>
    <p>Sim time: {s['time']:.2f}s | Challenge: {s['challenge']} | Completed: {completed}</p>
    <p>Reachy yaw: {s['reachy_yaw']:.3f} rad | LeKiwi: ({s['lekiwi_pos'][0]:.2f}, {s['lekiwi_pos'][1]:.2f})</p>
    <h2>Camera feeds</h2><ul>{cameras_html}</ul>
    <h2>Endpoints</h2>
    <ul>
      <li>GET  /cameras</li>
      <li>GET  /camera/{{name}} — JPEG snapshot</li>
      <li>GET  /state — JSON state</li>
      <li>POST /challenge/q1 — Reachy reads monitor</li>
      <li>POST /challenge/q2 — LeKiwi enters red zone</li>
      <li>POST /challenge/q3 — LeKiwi pries painting</li>
      <li>POST /reset — reset all robots</li>
    </ul>
    <h2>Flags (after solving)</h2>
    <ul>
      <li>Q1: captured via eye_camera when facing monitor</li>
      <li>Q2: captured via lekiwi_front in restricted zone</li>
      <li>Q3: captured via lekiwi_front at opened painting</li>
    </ul>
    </body></html>
    """

@app.get("/cameras")
def list_cameras():
    return {"cameras": CAMERA_NAMES}

@app.get("/camera/{name}")
def get_camera(name: str):
    if name not in CAMERA_NAMES:
        raise HTTPException(404, f"Unknown camera '{name}'. Available: {CAMERA_NAMES}")
    with frame_lock:
        data = latest_frames.get(name)
    if data is None:
        raise HTTPException(503, "Frame not ready yet — sim may still be starting")
    return Response(content=data, media_type="image/jpeg")

@app.get("/state")
def get_state():
    with state_lock:
        return dict(sim_state)

@app.post("/challenge/{q}")
def post_challenge(q: str):
    q = q.lower()
    if q not in ("q1", "q2", "q3"):
        raise HTTPException(400, "Challenge must be q1, q2, or q3")
    try:
        cmd_queue.put_nowait({"type": "challenge", "q": q})
    except queue.Full:
        raise HTTPException(409, "A command is already queued — try again shortly")
    return {"queued": q}

@app.post("/reset")
def post_reset():
    try:
        cmd_queue.put_nowait({"type": "reset"})
    except queue.Full:
        raise HTTPException(409, "A command is already queued — try again shortly")
    return {"queued": "reset"}

# ── MuJoCo sim (runs on main thread) ─────────────────────────────────────────
def run_sim():
    m = mujoco.MjModel.from_xml_path("scene.xml")
    d = mujoco.MjData(m)

    # Joint addresses
    yaw_jid   = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "yaw_body")
    YAW_ADR   = m.jnt_qposadr[yaw_jid]
    hinge_jid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "frame_hinge")
    HINGE_ADR = m.jnt_qposadr[hinge_jid]
    for i in range(m.njnt):
        if m.jnt_type[i] == 0:
            FREE_ADR    = m.jnt_qposadr[i]
            FREE_DOFADR = m.jnt_dofadr[i]
            break

    lekiwi_id = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "base_plate_layer_1_link")

    renderers = {c: mujoco.Renderer(m, height=480, width=640) for c in CAMERA_NAMES}
    completed = []

    def step(n=1):
        for _ in range(n):
            mujoco.mj_step(m, d)

    def render_all():
        for name, r in renderers.items():
            r.update_scene(d, camera=name)
            img = r.render()
            buf = io.BytesIO()
            Image.fromarray(img).save(buf, format="JPEG", quality=85)
            with frame_lock:
                latest_frames[name] = buf.getvalue()

    def update_state(challenge=None):
        lpos = d.xpos[lekiwi_id]
        with state_lock:
            sim_state["time"]       = float(d.time)
            sim_state["reachy_yaw"] = float(d.qpos[YAW_ADR])
            sim_state["lekiwi_pos"] = [float(lpos[0]), float(lpos[1])]
            sim_state["challenge"]  = challenge
            sim_state["completed"]  = list(completed)

    def lekiwi_glide(tx, ty, n_steps=6000, substeps=50, viewer=None):
        sx = float(d.qpos[FREE_ADR])
        sy = float(d.qpos[FREE_ADR + 1])
        sz = float(d.qpos[FREE_ADR + 2])
        segs = n_steps // substeps
        for k in range(segs + 1):
            t = k / segs
            d.qpos[FREE_ADR]     = sx + t * (tx - sx)
            d.qpos[FREE_ADR + 1] = sy + t * (ty - sy)
            d.qpos[FREE_ADR + 2] = sz
            d.qvel[FREE_DOFADR:FREE_DOFADR + 3] = 0.0
            step(substeps)
            if viewer:
                viewer.sync()
            render_all()

    def do_q1(viewer=None):
        print("[Q1] Reachy → Monitor")
        d.ctrl[0] = 2.50
        for _ in range(6):
            step(500)
            if viewer: viewer.sync()
            render_all()
        for i in range(1, 7):
            d.ctrl[i] = 0.15
        step(1000)
        if viewer: viewer.sync()
        render_all()
        # save flag frame
        os.makedirs("frames", exist_ok=True)
        r = renderers["eye_camera"]
        r.update_scene(d, camera="eye_camera")
        Image.fromarray(r.render()).save("frames/eye_camera_q1_monitor.png")
        print("  Saved frames/eye_camera_q1_monitor.png")

    def do_q2(viewer=None):
        print("[Q2] LeKiwi → Restricted zone (1.2, -0.8)")
        lekiwi_glide(1.2, -0.8, n_steps=6000, viewer=viewer)
        step(500)
        if viewer: viewer.sync()
        render_all()
        os.makedirs("frames", exist_ok=True)
        r = renderers["lekiwi_front"]
        r.update_scene(d, camera="lekiwi_front")
        Image.fromarray(r.render()).save("frames/lekiwi_front_q2_restricted.png")
        print("  Saved frames/lekiwi_front_q2_restricted.png")

    def do_q3(viewer=None):
        print("[Q3] LeKiwi → Painting (1.0, 1.2)")
        lekiwi_glide(1.0, 1.2, n_steps=6000, viewer=viewer)
        step(500)
        if viewer: viewer.sync()
        # pry open
        for angle in [i * 0.05 for i in range(29)]:
            d.qpos[HINGE_ADR] = angle
            step(30)
            if viewer: viewer.sync()
        render_all()
        os.makedirs("frames", exist_ok=True)
        r = renderers["lekiwi_front"]
        r.update_scene(d, camera="lekiwi_front")
        Image.fromarray(r.render()).save("frames/lekiwi_front_q3_painting.png")
        print("  Saved frames/lekiwi_front_q3_painting.png")

    def do_reset(viewer=None):
        print("[RESET]")
        for i in range(m.nu):
            d.ctrl[i] = 0.0
        # Reachy back to yaw=0
        d.ctrl[0] = 0.0
        # LeKiwi back to start
        d.qpos[FREE_ADR]     = 0.5
        d.qpos[FREE_ADR + 1] = 0.3
        d.qpos[FREE_ADR + 2] = 0.035
        d.qpos[FREE_ADR + 3:FREE_ADR + 7] = [1, 0, 0, 0]  # identity quaternion
        d.qvel[FREE_DOFADR:FREE_DOFADR + 6] = 0.0
        # painting hinge back
        d.qpos[HINGE_ADR] = 0.0
        step(500)
        if viewer: viewer.sync()
        render_all()
        completed.clear()

    # ── Main sim loop ─────────────────────────────────────────────────────────
    with mujoco.viewer.launch_passive(m, d) as v:
        print("Settling physics...")
        step(300)
        v.sync()
        render_all()
        update_state()
        print("Sim ready. API available at http://localhost:8000")

        RENDER_EVERY = 10  # render cameras every N steps (keep API fresh)
        tick = 0

        while v.is_running():
            # Process one queued command (non-blocking)
            try:
                cmd = cmd_queue.get_nowait()
            except queue.Empty:
                cmd = None

            if cmd:
                ctype = cmd["type"]
                update_state(challenge=ctype if ctype != "reset" else None)
                if ctype == "challenge":
                    q = cmd["q"]
                    if q == "q1":
                        do_q1(v)
                        completed.append("q1")
                    elif q == "q2":
                        do_q2(v)
                        completed.append("q2")
                    elif q == "q3":
                        do_q3(v)
                        completed.append("q3")
                elif ctype == "reset":
                    do_reset(v)
                update_state(challenge=None)
            else:
                mujoco.mj_step(m, d)
                v.sync()
                tick += 1
                if tick % RENDER_EVERY == 0:
                    render_all()
                    update_state()

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Start API in a background daemon thread
    api_thread = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning"),
        daemon=True,
    )
    api_thread.start()
    print("API server starting on http://localhost:8000")

    # MuJoCo sim runs on main thread (required for macOS GL context)
    run_sim()
