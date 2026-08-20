"""
CTF Robot Control — drive Reachy Mini and LeKiwi to capture flags.

Scene layout (top-down, Y forward):

        desk (-1.0, 0.8)        painting (1.0, 1.2)
              [Q1]                    [Q3]

        Reachy (0, 0)      LeKiwi (0.5, 0.3)

                        restricted (1.2, -0.8)
                               [Q2]

Actuators:
  [0]   yaw_body      Reachy body rotation (rad) — position actuator, kp=10
  [1-6] stewart_1-6   Reachy head tilt (Stewart neck)
  [7]   right_antenna
  [8]   left_antenna
  [9]   base_left_wheel   LeKiwi velocity (rad/s) — NOT used (unstable physics)
  [10]  base_right_wheel
  [11]  base_back_wheel

LeKiwi movement uses freejoint position interpolation instead of wheel actuators
because the omni-wheel physics becomes numerically unstable at any useful speed.

Run from ctf_scene/:
    mjpython robot_control.py
"""
import time
import math
import os
import subprocess
import mujoco
import mujoco.viewer
from PIL import Image

# ── Load ─────────────────────────────────────────────────────────────────────
m = mujoco.MjModel.from_xml_path("scene.xml")
d = mujoco.MjData(m)
os.makedirs("frames", exist_ok=True)

renderers = {
    "eye_camera":   mujoco.Renderer(m, height=480, width=640),
    "lekiwi_front": mujoco.Renderer(m, height=480, width=640),
    "cam_overview": mujoco.Renderer(m, height=480, width=640),
}

# ── Joint addresses ───────────────────────────────────────────────────────────
yaw_jid  = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "yaw_body")
YAW_ADR  = m.jnt_qposadr[yaw_jid]

hinge_jid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "frame_hinge")
HINGE_ADR = m.jnt_qposadr[hinge_jid]

# LeKiwi freejoint: qpos[FREE_ADR:FREE_ADR+7] = xyz + quaternion
# LeKiwi freejoint: qvel[FREE_DOFADR:FREE_DOFADR+6] = 6 DOFs
for i in range(m.njnt):
    if m.jnt_type[i] == 0:  # mjtJoint.mjJNT_FREE
        FREE_ADR    = m.jnt_qposadr[i]
        FREE_DOFADR = m.jnt_dofadr[i]
        break

# ── Utilities ─────────────────────────────────────────────────────────────────
def step(n=1, viewer=None):
    for _ in range(n):
        mujoco.mj_step(m, d)
    if viewer is not None:
        viewer.sync()

def capture(camera: str, tag: str = ""):
    r = renderers[camera]
    r.update_scene(d, camera=camera)
    img = r.render()
    path = f"frames/{camera}{'_' + tag if tag else ''}.png"
    Image.fromarray(img).save(path)
    print(f"  captured: {path}")
    return img

# ── Reachy Mini ───────────────────────────────────────────────────────────────
# At yaw=0 Reachy faces world +X. Desk is at (-1.0, 0.8) from origin.
# Empirically measured: yaw=2.50 points camera toward desk (dot=0.999).
# Positive yaw = CCW rotation when viewed from above.
REACHY_YAW_TO_DESK   = 2.50   # face toward desk at (-1.0, 0.8)
REACHY_HEAD_PITCH    = 0.15   # tilt head down toward monitor screen

def reachy_rotate_to_desk(viewer=None):
    print("  Reachy: rotating body toward desk (yaw=2.50)...")
    d.ctrl[0] = REACHY_YAW_TO_DESK
    # kp=10 is slow — need ~3000 steps to settle, sync every 500 so viewer updates
    for _ in range(6):
        step(500, viewer)

def reachy_tilt_head(viewer=None):
    print("  Reachy: tilting head down at monitor screen...")
    for i in range(1, 7):
        d.ctrl[i] = REACHY_HEAD_PITCH
    step(1000, viewer)

def reachy_reset(viewer=None):
    print("  Reachy: resetting to neutral pose...")
    for i in range(9):
        d.ctrl[i] = 0.0
    step(2000, viewer)

# ── LeKiwi smooth movement ────────────────────────────────────────────────────
# Wheel actuators cause numerical instability at any useful speed.
# Instead we directly interpolate the freejoint position (xyz) at a human-visible
# pace (~5 sim seconds for a 1-meter move).

def lekiwi_move_to(tx, ty, viewer=None, n_steps=5000, n_substeps=50):
    """Smoothly move LeKiwi to world position (tx, ty) by interpolating freejoint."""
    sx = float(d.qpos[FREE_ADR])
    sy = float(d.qpos[FREE_ADR + 1])
    sz = float(d.qpos[FREE_ADR + 2])
    # keep quaternion fixed (no rotation)
    dist = math.sqrt((tx - sx)**2 + (ty - sy)**2)
    print(f"  LeKiwi: ({sx:.2f},{sy:.2f}) → ({tx:.2f},{ty:.2f})  dist={dist:.2f}m")

    segments = n_steps // n_substeps
    for k in range(segments + 1):
        t = k / segments
        d.qpos[FREE_ADR]     = sx + t * (tx - sx)
        d.qpos[FREE_ADR + 1] = sy + t * (ty - sy)
        d.qpos[FREE_ADR + 2] = sz          # keep height
        # zero translational velocity so robot glides, not bounces
        d.qvel[FREE_DOFADR:FREE_DOFADR + 3] = 0.0
        step(n_substeps, viewer)

# ── Painting hinge ────────────────────────────────────────────────────────────
def pry_painting_open(viewer=None):
    print("  Prying painting frame open...")
    for angle in [i * 0.05 for i in range(29)]:  # 0 → 1.40 rad
        d.qpos[HINGE_ADR] = angle
        step(30, viewer)

# ── CTF challenges ────────────────────────────────────────────────────────────

def challenge_q1(viewer=None):
    """Q1: Reachy Mini reads the monitor screen flag."""
    print("\n[Q1] Reachy Mini → Monitor screen at (-1.0, 0.8)")
    reachy_rotate_to_desk(viewer)
    reachy_tilt_head(viewer)
    capture("eye_camera",   "q1_monitor")
    capture("cam_overview", "q1_overview")

def challenge_q2(viewer=None):
    """Q2: LeKiwi drives into the restricted (red) zone."""
    print("\n[Q2] LeKiwi → Restricted zone at (1.2, -0.8)")
    lekiwi_move_to(1.2, -0.8, viewer, n_steps=6000)
    step(500, viewer)   # pause in the zone
    capture("lekiwi_front", "q2_restricted")
    capture("cam_overview", "q2_overview")

def challenge_q3(viewer=None):
    """Q3: LeKiwi drives to the painting and pries the frame open."""
    print("\n[Q3] LeKiwi → Painting zone at (1.0, 1.2)")
    lekiwi_move_to(1.0, 1.2, viewer, n_steps=6000)
    step(500, viewer)   # pause before prying
    pry_painting_open(viewer)
    step(300, viewer)
    capture("lekiwi_front", "q3_painting")
    capture("cam_overview", "q3_overview")

# ── Main ─────────────────────────────────────────────────────────────────────
print("CTF Robot Control")
print("=================")
print("Scene layout:")
print("  desk       (-1.0,  0.8)  [Q1]")
print("  painting   ( 1.0,  1.2)  [Q3]")
print("  Reachy     ( 0.0,  0.0)")
print("  LeKiwi     ( 0.5,  0.3)")
print("  restricted ( 1.2, -0.8)  [Q2 — red zone]")
print()

with mujoco.viewer.launch_passive(m, d) as v:
    print("Settling physics (300 steps)...")
    step(300, v)

    challenge_q1(v)

    print("\nResetting Reachy to neutral...")
    reachy_reset(v)

    challenge_q2(v)

    challenge_q3(v)

    # Open flag frames in Preview
    frames = [
        "frames/eye_camera_q1_monitor.png",
        "frames/lekiwi_front_q2_restricted.png",
        "frames/lekiwi_front_q3_painting.png",
        "frames/cam_overview_q3_overview.png",
    ]
    existing = [f for f in frames if os.path.exists(f)]
    if existing:
        subprocess.Popen(["open", "-a", "Preview"] + existing)
        print(f"\nOpened {len(existing)} flag frames in Preview.")

    print("\nDone. Close viewer to quit.")
    while v.is_running():
        mujoco.mj_step(m, d)
        v.sync()
        time.sleep(0.002)
