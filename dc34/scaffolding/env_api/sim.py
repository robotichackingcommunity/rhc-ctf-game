"""
sim.py — MuJoCo sim loop + shared state for the CTF env API.

Exports:
  frame_lock, latest_frames   — camera JPEG bytes (written by sim thread)
  state_lock, sim_state       — robot positions + challenge status
  cmd_queue                   — send {"type": "challenge", "q": "q1"|"q2"|"q3_approach"|"q3"}
                                  or {"type": "reset"}
                                  or {"type": "arm_move", "targets": {joint_name: angle_rad, ...}}
                                  or {"type": "arm_home"}
                                  or {"type": "arm2_move", "targets": {joint_name: angle_rad, ...}}
                                  or {"type": "arm2_home"}
  policy_queue                — Q5 one-shot ACT-policy replay: put {"actions": [...], "fps": f}
  run_sim()                   — blocking; must run on the main thread (macOS GL)

The CTF game API (app.py) runs on a background thread and reads/writes these
shared objects. The sim thread is the only writer to latest_frames and sim_state.
"""
import io
import math
import os
import queue
import threading
import time

import mujoco
import mujoco.viewer
from PIL import Image

# ── Shared state (module-level singletons) ────────────────────────────────────
frame_lock = threading.Lock()
latest_frames: dict[str, bytes] = {}   # camera_name → JPEG bytes

# ── Active-camera gating ──────────────────────────────────────────────────────
# The render worker only renders cameras that currently have a live consumer, so
# during streaming it pays the ~45-90ms/camera GL cost only for cameras someone
# is actually watching (notably skipping the expensive q5_desk_cam whenever no
# authorized MJPEG viewer is subscribed — this gates RENDERING only; q5_desk
# delivery gating in app.py is unchanged, so no token leak). WS/MJPEG handlers
# register interest on connect and drop it on disconnect.
active_lock = threading.Lock()
_camera_refcount: dict[str, int] = {}   # camera name → live streaming-consumer count
# Transient interest: camera name → monotonic expiry. Used by short-lived,
# non-streaming readers (the token-gated q5_desk GET, which reads a frame once
# per poll without holding a long-lived subscription) to keep the render worker
# rendering a camera for a few seconds. Expiry-based, so repeated polls just
# refresh the deadline instead of leaking refcounts. Guarded by active_lock.
_camera_transient: dict[str, float] = {}

# Signals the render worker that a fresh snapshot (or a new consumer) is ready.
# Defined at module scope — not inside run_sim() — so app.py's connect handlers
# can wake the worker for a prompt first frame via subscribe_cameras().
render_request = threading.Event()

# Push hook: app.py installs a no-arg callable that bridges "a render pass just
# finished" into its asyncio loop (loop.call_soon_threadsafe), so the WS/MJPEG
# handlers can await a frame instead of polling. None until app.py wires it.
frame_ready_hook = None

# EWMA (seconds) of one render pass's wall-time, updated by the render worker
# and read by step() to pace each motion segment to about one pass — so the feed
# samples every segment instead of physics racing ahead. Single writer / single
# reader float, so no lock. None until the first pass is measured.
render_pass_ewma = None
_RENDER_EWMA_ALPHA = 0.2

_IDLE_RENDER_INTERVAL = 2.0   # seconds; with no consumer subscribed the worker
                              # renders every camera at most this often, so a
                              # one-shot GET /cameras or a fresh connect never
                              # reads a stale-forever frame (and it spends almost
                              # no GL time while nobody is watching).


def subscribe_cameras(names) -> None:
    """Register a live streaming consumer for each camera in names (WS/MJPEG
    connect). Wakes the render worker so the new consumer gets a frame promptly."""
    with active_lock:
        for name in names:
            _camera_refcount[name] = _camera_refcount.get(name, 0) + 1
    render_request.set()


def touch_camera(name, ttl: float = 3.0) -> None:
    """Register transient interest in one camera for ttl seconds (non-streaming
    readers). Wakes the render worker so this poll's interest lands promptly."""
    with active_lock:
        _camera_transient[name] = time.monotonic() + ttl
    render_request.set()


def unsubscribe_cameras(names) -> None:
    """Drop one streaming consumer per camera (WS/MJPEG disconnect)."""
    with active_lock:
        for name in names:
            n = _camera_refcount.get(name, 0) - 1
            if n > 0:
                _camera_refcount[name] = n
            else:
                _camera_refcount.pop(name, None)

state_lock = threading.Lock()
sim_state: dict = {
    "time":       0.0,
    "reachy_yaw": 0.0,
    "lekiwi_pos": [0.0, 0.0],
    "lekiwi_arm": {},       # joint name -> current angle (rad)
    "arm2":       {},       # Q5 desk arm — joint name -> current angle (rad)
    "q5":         {},       # Q5 cube-sorting — {"solved": bool, "zones": [{target,current_sum,solved}]}
    "challenge":  None,    # "q1" | "q2" | "q3" | None
    "completed":  [],
}

# LeKiwi's SO-ARM100 joints, in the order sent for "arm_move" commands.
# The scene attaches so_arm100.xml with prefix="arm_" (an empty prefix corrupts
# unrelated materials elsewhere in the scene — see scene.xml's <attach> comment),
# so the actual mujoco names are "arm_Rotation" etc; ARM_JOINTS stays unprefixed
# as the public-facing joint name set used by app.py and sim_state.
ARM_JOINTS = ["Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll", "Jaw"]
_ARM_PREFIX = "arm_"

# Q5 desk arm — a second, independently-actuated SO-ARM100 fixed to its own desk
# (poisoning_desk in scene.xml), attached with prefix="arm2_". Same joint set as
# the LeKiwi arm, controlled separately via "arm2_move"/"arm2_home" commands.
ARM2_JOINTS = ARM_JOINTS
_ARM2_PREFIX = "arm2_"

# maxsize=1: one in-flight command at a time; app layer gets 409 if busy
cmd_queue: queue.Queue = queue.Queue(maxsize=1)


class Aborted(Exception):
    """Raised out of step() when abort_event is set, unwinding whichever blocking
    command helper is mid-motion so the sim loop can service a reset instead."""


# Cooperative abort for the in-flight command. /scene/reset sets this (after
# draining cmd_queue) so a long blocking helper — a LeKiwi route, a Q1/Q2/Q3
# animation — yields at its next step() batch instead of holding the single
# cmd_queue slot until it finishes. The sim thread clears it right before
# running do_reset, which must not abort itself. Only helps a sim that is still
# stepping: a truly frozen loop (GL stall, dead thread) still needs a restart.
abort_event = threading.Event()

# Monotonic timestamp of the last completed step()/idle tick, written by the sim
# thread and read by app.py for /scene/state's sim_alive. Bumped INSIDE step(),
# so a legitimately long command keeps it fresh and the staleness threshold is
# independent of how long commands legitimately run. 0.0 = not stepping yet
# (sim still booting), which app.py deliberately does not report as a stall.
heartbeat: dict = {"last_tick": 0.0}

# Bumped once per COMPLETED do_reset, so /scene/reset can report that the scene was
# actually restored instead of inferring it from the heartbeat. Positive evidence:
# the heartbeat only says the sim is stepping, and it reads "alive" for the first
# _SIM_STALL_SECS after a sim dies (and forever if it died before its first tick).
reset_ack: dict = {"count": 0}

# Instant geom color changes — processed every tick, never blocks
color_queue: queue.Queue = queue.Queue()

# Restricted-zone unlock signal (bool) — processed every tick, never blocks.
# Drives whether the Q2 flag texture or the plain red banner is shown.
lock_queue: queue.Queue = queue.Queue()

# Painting-zone solved signal (bool) — processed every tick, never blocks.
# Drives the painting_floor marker's color (blue = unsolved, green = flag caught).
painting_solve_queue: queue.Queue = queue.Queue()

# Reachy-compromised signal (bool) — processed every tick, never blocks.
# Drives whether the Q1 monitor shows a black "off" screen or the real password,
# so cameras other than the intended eye_camera can't just read it off unsolved.
screen_unlock_queue: queue.Queue = queue.Queue()

# ── Q5 cube-sorting: ACT-policy replay + zone state machine ───────────────────
# LeRobot teleop-episode action field -> arm2 MuJoCo joint. base_x/base_y/
# base_theta are intentionally absent (desk arm is fixed — those are ignored).
POLICY_ACTION_MAP = {
    "arm_shoulder_pan":  "Rotation",
    "arm_shoulder_lift": "Pitch",
    "arm_elbow_flex":    "Elbow",
    "arm_wrist_flex":    "Wrist_Pitch",
    "arm_wrist_roll":    "Wrist_Roll",
    "arm_gripper":       "Jaw",
}

# Published by app.py at /scene/state.policy_spec so players don't guess the
# schema. Actions are 0-100 percent; the sim maps each to the joint's actuator
# range: rad = lo + (pct/100)*(hi-lo). Ranges here mirror so_arm100.xml (the
# live sim maps against actuator_ctrlrange, which is these values).
POLICY_SPEC = {
    "format":         "lerobot_episode_json",
    "fps_default":    30.0,
    "action_fields":  list(POLICY_ACTION_MAP.keys()),
    "ignored_fields": ["base_x", "base_y", "base_theta"],
    "joint_map":      dict(POLICY_ACTION_MAP),
    "action_units":   "0-100 percent of joint range; rad = lo + (pct/100)*(hi-lo)",
    "joint_ranges_rad": {
        "Rotation":    [-1.92, 1.92],
        "Pitch":       [-1.747, 1.747],
        "Elbow":       [-1.657, 1.657],
        "Wrist_Pitch": [-1.66, 1.66],
        "Wrist_Roll":  [-2.79, 2.79],
        "Jaw":         [0.0, 0.6],
    },
    "max_frames":     5000,
}

# Cube body name -> value (leetspeak-digit sum puzzle; see the Q5 design doc).
Q5_CUBES = [
    ("q5_cube_red_1",   2), ("q5_cube_red_2",   2),
    ("q5_cube_blue_1",  4), ("q5_cube_blue_2",  4),
    ("q5_cube_green_1", 5), ("q5_cube_green_2", 5),
]

# Zone target sums + world-XY boxes + check-mark geom (from the scene build).
# Coordinates are TUNE-on-live-sim values matching scene.xml's zone tiles
# (0.10 half-extent around world centers -1.82 / -1.60 / -1.38, y center -1.12).
Q5_ZONES = [
    {"target": 2,  "xmin": -1.92, "xmax": -1.72, "ymin": -1.22, "ymax": -1.02, "check": "q5_zone1_check"},
    {"target": 9,  "xmin": -1.70, "xmax": -1.50, "ymin": -1.22, "ymax": -1.02, "check": "q5_zone2_check"},
    {"target": 14, "xmin": -1.48, "xmax": -1.28, "ymin": -1.22, "ymax": -1.02, "check": "q5_zone3_check"},
]

# Tuning constants (physics untestable here — TUNE on the live sim):
Q5_REST_Z          = 0.55   # cube-center z when resting on the desk (desk 0.53 + half 0.02)
Q5_REST_TOL        = 0.02   # |cz - REST_Z| within this counts as "resting" (excludes held/airborne)
Q5_WELD_TOL        = 0.05   # fingertip-to-cube-center distance (m) at which a closed grip grabs
Q5_GRIP_CLOSED_RAD = 0.15   # Jaw ctrl (rad) at/below this = closed grip; POLARITY may need flipping

# Off-desk escape watchdog: a (non-held) cube outside the desk-top footprint or
# fallen below it snaps back to its start pose. Bounds = desk center (-1.6,-1.3)
# +- top half-extents (0.4, 0.35), plus a small margin for wall-pinned cubes.
Q5_DESK_XMIN, Q5_DESK_XMAX = -2.03, -1.17
Q5_DESK_YMIN, Q5_DESK_YMAX = -1.68, -0.92
Q5_OFFDESK_Z       = 0.50   # cube center below this = fell off the desk

# Goal button (world pos of the cap top, matching scene.xml's q5_button_* geoms
# at desk-local (0.3, 0)). Armed + shown once all 3 zones latch; a fingertip
# within Q5_BUTTON_TOL of the cap presses it, which finally reveals the flag.
Q5_BUTTON_POS      = (-1.30, -1.30, 0.56)
Q5_BUTTON_TOL      = 0.04
Q5_DESK_RGBA        = [0.5, 0.3, 0.2, 1.0]    # normal desk-top (and flag-cover) color
Q5_DESK_SOLVED_RGBA = [0.2, 0.65, 0.3, 1.0]   # all-zones-latched "goal" color

# Button safety interlock (the Q5 "model poisoning" mechanic). The arm can reach
# the button once zones latch, but a safety-rated interlock refuses to ACTUATE it
# unless the loaded policy disengages the interlock. The interlock state is read
# from the uploaded policy's own metadata (app.py: episode.metadata.safety
# .button_interlock) — a model-supplied safety parameter, hence poisonable: an
# attacker who controls the model controls the "safety". Default is ENGAGED, so a
# naive press (or the provided default press policy, which only hovers on top) is
# blocked; the solve must both reach the button AND supply a disengaged policy.
Q5_INTERLOCK_ENGAGED    = "engaged"
Q5_INTERLOCK_DISENGAGED = "disengaged"

# One-shot policy replay request — app.py puts {"actions": [...], "fps": f}.
# Drained per tick like color_queue; never blocks cmd_queue.
policy_queue: queue.Queue = queue.Queue()

_SCENE_XML = os.path.join(os.path.dirname(__file__), "../scene/scene.xml")
_CAMERA_NAMES = ["eye_camera", "lekiwi_front", "cam_overview", "arm_wrist", "arm2_wrist", "q5_desk_cam"]
# Cameras players watch closely — rendered on every subscribed pass. The rest
# (eye_camera, cam_overview) render only every 3rd pass, dropping the average
# subscribed-pass cost from 6 to ~4.67 renders while keeping these responsive.
_PRIORITY_CAMERAS = {"lekiwi_front", "arm_wrist", "arm2_wrist", "q5_desk_cam"}

# The passive viewer is a DEVELOPMENT convenience. On a headless game server it
# software-rasterises (llvmpipe) into an Xvfb display nobody can ever look at, and
# it is not cheap: measured on a g4dn.xlarge (4 vCPU), the main thread sits in
# viewer.sync() at ~96% of a core while the viewer's own render thread burns
# another ~92%, plus four llvmpipe workers at ~14% each. That is roughly half the
# box, and because the sim loop blocks in sync() every tick it throttles the tick
# rate — which is what sets the camera frame rate.
#
# The offscreen camera renders do NOT need it: mujoco.Renderer builds its own EGL
# context on the GPU (verified: mujoco.egl.GLContext, ~2.4ms for a 1.23Mpx frame).
#
# So: off by default, since every deployed instance is headless. Set SIM_VIEWER=1
# for a local macOS/desktop run where you actually want the window.
_VIEWER_ENABLED = os.environ.get("SIM_VIEWER", "0") == "1"


class _NoViewer:
    """No-op stand-in for the passive viewer handle, for headless runs.

    Must mirror every attribute run_sim() and the motion helpers use on the handle
    — currently is_running() and sync() — and stay truthy, since helpers guard with
    `if viewer:`. test_viewer_headless.py greps sim.py for `v.<attr>` and asserts
    this class covers all of them, so adding a new viewer call without updating
    this class fails the tests rather than the game server at boot.
    """

    def is_running(self) -> bool:
        """The real handle reports False once the window is closed; headless has no
        window, so the sim loop should run until the process is stopped."""
        return True

    def sync(self) -> None:
        pass

    def __enter__(self) -> "_NoViewer":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _viewer_context(m, d):
    """Launch the passive viewer only when explicitly asked for; otherwise hand
    back a no-op so nothing tries to open a GL window on a headless box."""
    if _VIEWER_ENABLED:
        return mujoco.viewer.launch_passive(m, d)
    return _NoViewer()


def run_sim() -> None:
    """Blocking sim loop. Call from the main thread."""
    m = mujoco.MjModel.from_xml_path(_SCENE_XML)
    d = mujoco.MjData(m)

    # ── Joint addresses ───────────────────────────────────────────────────────
    yaw_jid   = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "yaw_body")
    YAW_ADR   = m.jnt_qposadr[yaw_jid]
    hinge_jid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, "frame_hinge")
    HINGE_ADR = m.jnt_qposadr[hinge_jid]
    HINGE_DOFADR = m.jnt_dofadr[hinge_jid]
    # LeKiwi's freejoint — pinned to its body, not "first free joint in model":
    # Q5 adds 6 cube freejoints that sort BEFORE LeKiwi's in model order, so a
    # bare first-free-joint scan would now grab a cube and break LeKiwi motion.
    _lekiwi_bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "base_plate_layer_1_link")
    FREE_ADR = FREE_DOFADR = None
    for i in range(m.njnt):
        if m.jnt_type[i] == 0 and m.jnt_bodyid[i] == _lekiwi_bid:   # mjJNT_FREE on LeKiwi
            FREE_ADR    = m.jnt_qposadr[i]
            FREE_DOFADR = m.jnt_dofadr[i]
            break

    arm_actuator_id = {name: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_ACTUATOR, _ARM_PREFIX + name) for name in ARM_JOINTS}
    arm_joint_id    = {name: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, _ARM_PREFIX + name) for name in ARM_JOINTS}
    arm_home_key_id = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_KEY, _ARM_PREFIX + "home")

    arm2_actuator_id = {name: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_ACTUATOR, _ARM2_PREFIX + name) for name in ARM2_JOINTS}
    arm2_joint_id    = {name: mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, _ARM2_PREFIX + name) for name in ARM2_JOINTS}
    arm2_home_key_id = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_KEY, _ARM2_PREFIX + "home")

    # Q5 cube-sorting addresses (cubes are top-level freejoint bodies).
    cube_qposadr: dict[str, int] = {}
    cube_dofadr:  dict[str, int] = {}
    for name, _val in Q5_CUBES:
        jid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, name + "_joint")
        cube_qposadr[name] = m.jnt_qposadr[jid]
        cube_dofadr[name]  = m.jnt_dofadr[jid]
    # Start poses = the XML-authored freejoint qpos (pos + identity quat), snapshot
    # before any stepping so /scene/reset can restore the exact opening layout.
    cube_start_qpos = {name: d.qpos[cube_qposadr[name]:cube_qposadr[name] + 7].copy()
                       for name, _val in Q5_CUBES}
    zone_check_id = [mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, z["check"]) for z in Q5_ZONES]
    flag_cover_id = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "q5_flag_cover")
    fingertip_sid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, "arm2_fingertip")
    desk_top_id     = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "poisoning_desk_top")
    button_base_id  = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "q5_button_base")
    button_cap_id   = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "q5_button_cap")

    lekiwi_id         = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "base_plate_layer_1_link")
    restricted_geom_id = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "restricted_floor")
    painting_floor_id  = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "painting_floor")
    restricted_flag_id   = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "restricted_flag")
    restricted_banner_id = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "restricted_flag_banner")
    screen_off_id        = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, "screen_off")
    # arm_wrist declares resolution="480 640" (portrait) in so_arm100.xml — render it
    # at its native aspect ratio instead of the shared 640x480 landscape, which was
    # stretching/squishing the image and making the view hard to read.
    # Only the three cameras players actually watch closely (both wrists + the
    # Q5 playfield) get the expensive high-res render; the rest stay at the
    # cheap fallback below.
    _CAMERA_SIZE = {"arm_wrist": (1280, 960), "arm2_wrist": (1280, 960), "q5_desk_cam": (960, 1280)}  # (height, width)

    # Camera capture runs on its own thread so a ~350ms 6-camera render/JPEG
    # pass never blocks mj_step/viewer.sync() — that blocking was the actual
    # cause of choppy LeKiwi/arm motion, not physics speed. The render thread
    # gets its own MjData (qpos/qvel snapshot + mj_forward to recompute the
    # kinematics rendering reads) and its own Renderer set; it only ever
    # reads m, never writes it. Snapshotting qpos/qvel directly (rather than
    # mujoco.mj_copyData, which some installed mujoco builds lack) keeps this
    # portable across mujoco versions.
    render_data = mujoco.MjData(m)
    render_data_lock = threading.Lock()  # guards render_data against a torn read
                                          # while the snapshot is being written
    render_stop = threading.Event()       # render_request lives at module scope

    def _render_worker() -> None:
        global render_pass_ewma
        # One Renderer per distinct (height, width), shared across every camera
        # of that size (update_scene()+render() per camera still runs, but skips
        # re-creating the OpenGL context each time — that context switch, not
        # resolution, is most of render()'s ~45-90ms/camera cost). Cameras must
        # match exactly: rendering at a larger size and cropping down changes
        # the effective field of view, so no cross-size sharing.
        renderer_by_size: dict[tuple[int, int], mujoco.Renderer] = {}
        cameras_by_size: dict[tuple[int, int], list[str]] = {}
        for c in _CAMERA_NAMES:
            size = _CAMERA_SIZE.get(c, (480, 640))
            cameras_by_size.setdefault(size, []).append(c)
        for size in cameras_by_size:
            renderer_by_size[size] = mujoco.Renderer(m, *size)

        last_idle_render = 0.0
        pass_count = 0
        while not render_stop.is_set():
            triggered = render_request.wait(timeout=_IDLE_RENDER_INTERVAL)
            render_request.clear()
            with active_lock:
                want = {c for c, n in _camera_refcount.items() if n > 0}
                # Fold in unexpired transient interest (gated q5_desk GET poll),
                # pruning stale entries so the set never grows without bound.
                now_t = time.monotonic()
                for c, exp in list(_camera_transient.items()):
                    if exp > now_t:
                        want.add(c)
                    else:
                        del _camera_transient[c]
            if want:
                # A consumer is subscribed — render only the cameras it watches,
                # but only when a fresh snapshot / new consumer triggered us; a
                # bare timeout with no new motion just holds the last frame.
                if not triggered:
                    continue
                # Priority cameras render every pass; non-priority (eye_camera,
                # cam_overview) only every 3rd, so at the console mix the pass
                # cost drops from 6 to ~4.67 renders on average.
                pass_count += 1
                if pass_count % 3 != 0:
                    want &= _PRIORITY_CAMERAS
            elif triggered:
                # No streaming consumer, but a fresh snapshot triggered us
                # (render_all() sets render_request on every motion segment):
                # refresh at render cadence so a solver driving the robot while
                # reading latest_frames WITHOUT subscribing — GET /cameras or
                # /robot/chat — never acts on a ~2s-stale frame mid-move.
                # q5_desk_cam is excluded here: it is reachable only via the
                # Q4-gated MJPEG path, which subscribes (landing in the branch
                # above) whenever an authorized viewer is present, so with no
                # subscriber there is no consumer that may see it.
                want = set(_CAMERA_NAMES) - {"q5_desk_cam"}
            else:
                # Truly quiet: no consumer AND no fresh motion. Render the
                # non-gated cameras on a slow idle cadence so a one-shot GET
                # /cameras or a fresh connect never reads a stale-forever frame
                # (q5_desk_cam stays skipped for the same gating reason above).
                now = time.monotonic()
                if now - last_idle_render < _IDLE_RENDER_INTERVAL:
                    continue
                last_idle_render = now
                want = set(_CAMERA_NAMES) - {"q5_desk_cam"}
            t_pass = time.perf_counter()
            with render_data_lock:
                mujoco.mj_forward(m, render_data)
            for size, names in cameras_by_size.items():
                r = renderer_by_size[size]
                for name in names:
                    if name not in want:
                        continue
                    with render_data_lock:
                        r.update_scene(render_data, camera=name)
                    # update_scene() re-enables shadows+reflections every call;
                    # disable both before rendering — those two passes re-rasterize
                    # the scene, and skipping them cuts a camera render 129→39ms (3.3x).
                    r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = 0
                    r.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = 0
                    img = r.render()
                    buf = io.BytesIO()
                    # quality=80 + 4:2:0 chroma subsampling roughly halves the
                    # encoded size vs near-lossless q95 for a live status feed,
                    # cutting encode time (paid per camera per pass) and the
                    # base64/WS/MJPEG payload — still plainly readable JPEG.
                    Image.fromarray(img).save(buf, format="JPEG", quality=80, subsampling=2)
                    with frame_lock:
                        latest_frames[name] = buf.getvalue()
            # Track this pass's wall-time so step() can pace motion to it.
            dt = time.perf_counter() - t_pass
            render_pass_ewma = (dt if render_pass_ewma is None
                                else (1 - _RENDER_EWMA_ALPHA) * render_pass_ewma
                                     + _RENDER_EWMA_ALPHA * dt)
            # Push: nudge the WS/MJPEG handlers awake now that this pass's frames
            # are in latest_frames, instead of making them poll for the change.
            hook = frame_ready_hook
            if hook is not None:
                hook()

    render_thread = threading.Thread(target=_render_worker, daemon=True)
    render_thread.start()

    completed: list[str] = []
    restricted_unlocked = False
    reachy_screen_unlocked = False

    # Q5 runtime state (mutated by nested helpers via nonlocal / mutable lists):
    held_cube: str | None = None          # freejoint body currently welded to the gripper
    replay: dict | None = None            # active one-shot policy replay, or None
    q5_latched = [False] * len(Q5_ZONES)  # sticky per-zone solved flags
    q5_sums    = [0] * len(Q5_ZONES)      # current value-sum of resting cubes per zone
    # Goal button state. "interlock" tracks the loaded policy's safety setting;
    # "blocked_by_safety" flips true when a reach-the-button attempt is refused
    # by an engaged interlock (a discoverability signal exposed in /scene/state).
    q5_button  = {"armed": False, "pressed": False,
                  "interlock": Q5_INTERLOCK_ENGAGED, "blocked_by_safety": False}
    q5_state   = {"solved": False}        # overall Q5 solved (all zones latched + button pressed)

    # ── Helpers ───────────────────────────────────────────────────────────────
    # Real-time pacing: physics runs far faster than the render thread can
    # sample it. Without pacing, an unpaced glide/move finishes before the
    # render thread gets even one frame in — the camera feed looks frozen, then
    # jumps to the end pose. So step() stretches physics pacing so one motion
    # segment (one render_all() call) takes about one render pass, keeping the
    # feed sampling each segment instead of racing ahead. The pass cost is now
    # measured live (render_pass_ewma) — since rendering only the watched
    # cameras made passes far cheaper than the old fixed ~350ms — and the target
    # is clamped to [_SIM_SPEED_FACTOR, 1.0]: never slower than the old floor,
    # never faster than real time. _SIM_SPEED_FACTOR is that floor / the
    # fallback used until the first pass is measured.
    _SIM_SPEED_FACTOR = 0.35
    _next_step_deadline = time.perf_counter()

    def step(n: int = 1) -> None:
        nonlocal _next_step_deadline
        # Abort granularity is one batch: a helper mid-motion yields here, at its
        # next step() call, rather than part-way through a batch.
        if abort_event.is_set():
            raise Aborted
        for _ in range(n):
            mujoco.mj_step(m, d)
        heartbeat["last_tick"] = time.monotonic()
        seg_sim = n * m.opt.timestep
        pass_wall = render_pass_ewma
        if pass_wall and pass_wall > 0.0:
            factor = max(_SIM_SPEED_FACTOR, min(1.0, seg_sim / pass_wall))
        else:
            factor = _SIM_SPEED_FACTOR   # no pass measured yet — use the floor
        _next_step_deadline += seg_sim / factor
        now = time.perf_counter()
        if _next_step_deadline > now:
            time.sleep(_next_step_deadline - now)
        else:
            _next_step_deadline = now

    def update_flag_visibility() -> None:
        """Show the Q2 flag texture only once unlocked AND LeKiwi is physically
        inside the restricted box; otherwise show the plain red banner."""
        lpos = d.xpos[lekiwi_id]
        fpos = d.geom_xpos[restricted_geom_id]
        hx, hy = m.geom_size[restricted_geom_id][0], m.geom_size[restricted_geom_id][1]
        inside = abs(lpos[0] - fpos[0]) <= hx and abs(lpos[1] - fpos[1]) <= hy
        show_flag = restricted_unlocked and inside
        m.geom_rgba[restricted_flag_id]   = [1, 1, 1, 1] if show_flag else [0, 0, 0, 0]
        m.geom_rgba[restricted_banner_id] = [0, 0, 0, 0] if show_flag else [1, 1, 1, 1]

    def update_screen_visibility() -> None:
        """Show the Q1 password texture only once Reachy has been prompt-injected
        (reachy_screen_unlocked) — otherwise cover it with a plain black screen so
        no other camera (LeKiwi, SO-ARM100 wrist) can just read it off directly."""
        m.geom_rgba[screen_off_id] = [0, 0, 0, 0] if reachy_screen_unlocked else [0.02, 0.02, 0.02, 1]

    def render_all() -> None:
        """Snapshot sim state and hand it to the render thread. Non-blocking:
        if the previous request hasn't been picked up yet, this frame's
        snapshot simply replaces it (dropped, not queued — camera feeds only
        need the latest state, not every intermediate one)."""
        update_flag_visibility()
        update_screen_visibility()
        with render_data_lock:
            render_data.qpos[:] = d.qpos
            render_data.qvel[:] = d.qvel
            render_data.time = d.time
        render_request.set()

    def update_state(challenge: str | None = None) -> None:
        lpos = d.xpos[lekiwi_id]
        with state_lock:
            sim_state["time"]       = float(d.time)
            sim_state["reachy_yaw"] = float(d.qpos[YAW_ADR])
            sim_state["lekiwi_pos"] = [float(lpos[0]), float(lpos[1])]
            sim_state["lekiwi_arm"] = {
                name: float(d.qpos[m.jnt_qposadr[jid]]) for name, jid in arm_joint_id.items()
            }
            sim_state["arm2"] = {
                name: float(d.qpos[m.jnt_qposadr[jid]]) for name, jid in arm2_joint_id.items()
            }
            sim_state["q5"] = {
                "solved": q5_state["solved"],
                "button": dict(q5_button),
                "zones": [
                    {"target": z["target"], "current_sum": q5_sums[i], "solved": q5_latched[i]}
                    for i, z in enumerate(Q5_ZONES)
                ],
                "replay": {
                    "active": replay is not None,
                    "frames_remaining": (len(replay["actions"]) - replay["idx"]) if replay else 0,
                    "total_frames": len(replay["actions"]) if replay else 0,
                    "fps": replay["fps"] if replay else 0.0,
                },
            }
            sim_state["challenge"]  = challenge
            sim_state["completed"]  = list(completed)

    # Solid obstacles with no legitimate bypass — a straight glide should stop
    # at them rather than teleport through, and the router below detours
    # around them via their corners. Q2's restricted_wall is included as a
    # safety net for direct moves (the router also handles the general case).
    _STATIC_OBSTACLE_BOXES = [
        (0.6, 1.8, -0.22, -0.18),        # Q2 restricted_wall
        (0.5, 1.5, 1.18, 1.22),          # Q3 painting wall
        (-2.55, 2.55, 2.975, 3.025),     # room boundary — north
        (-2.55, 2.55, -2.025, -1.975),   # room boundary — south
        (2.475, 2.525, -2.05, 3.05),     # room boundary — east
        (-2.525, -2.475, -2.05, 3.05),   # room boundary — west
        (-2.0, -1.2, -1.65, -0.95),      # Q5 poisoning_desk footprint
        (-2.27, -1.33, 1.78, 1.82),      # Q4 hint: soc_console panel footprint
    ]

    def _segment_box_intersection(sx: float, sy: float, tx: float, ty: float,
                                   box: tuple[float, float, float, float]) -> float | None:
        """Return the entry parameter t in [0,1] where segment (sx,sy)->(tx,ty)
        first enters box=(x0,x1,y0,y1), or None if it never does."""
        x0, x1, y0, y1 = box
        dx, dy = tx - sx, ty - sy
        t_enter, t_exit = 0.0, 1.0
        for s, d_, lo, hi in ((sx, dx, x0, x1), (sy, dy, y0, y1)):
            if abs(d_) < 1e-12:
                if s < lo or s > hi:
                    return None
            else:
                t0, t1 = (lo - s) / d_, (hi - s) / d_
                if t0 > t1:
                    t0, t1 = t1, t0
                t_enter, t_exit = max(t_enter, t0), min(t_exit, t1)
        if t_enter <= t_exit and 0.0 <= t_enter <= 1.0:
            return t_enter
        return None

    def _clip_to_static_walls(sx: float, sy: float, tx: float, ty: float) -> tuple[float, float]:
        """If the straight path (sx,sy)->(tx,ty) would enter one of the solid
        obstacle boxes above, stop it just short of the obstacle face instead."""
        dx, dy = tx - sx, ty - sy
        path_len = math.hypot(dx, dy)
        if path_len < 1e-9:
            return tx, ty
        best_t = 1.0
        for box in _STATIC_OBSTACLE_BOXES:
            t = _segment_box_intersection(sx, sy, tx, ty, box)
            if t is not None:
                best_t = min(best_t, t)
        if best_t >= 1.0:
            return tx, ty
        stop_t = max(0.0, best_t - 0.05 / path_len)  # stop 5cm short of the obstacle face
        return sx + stop_t * dx, sy + stop_t * dy

    def _line_of_sight(sx: float, sy: float, tx: float, ty: float) -> bool:
        return all(_segment_box_intersection(sx, sy, tx, ty, box) is None
                   for box in _STATIC_OBSTACLE_BOXES)

    def _first_blocking_box(sx: float, sy: float, tx: float, ty: float):
        best_t, best_box = None, None
        for box in _STATIC_OBSTACLE_BOXES:
            t = _segment_box_intersection(sx, sy, tx, ty, box)
            if t is not None and (best_t is None or t < best_t):
                best_t, best_box = t, box
        return best_box

    _ROUTE_CLEARANCE = 0.15  # keep LeKiwi's footprint clear of obstacle corners
    _ROUTE_MAX_HOPS = 12     # generous bound for this scene's handful of obstacles

    def _box_corners(box: tuple[float, float, float, float], clearance: float) -> list[tuple[float, float]]:
        x0, x1, y0, y1 = box
        return [
            (x0 - clearance, y0 - clearance),
            (x0 - clearance, y1 + clearance),
            (x1 + clearance, y0 - clearance),
            (x1 + clearance, y1 + clearance),
        ]

    def _route_to(sx: float, sy: float, tx: float, ty: float) -> list[tuple[float, float]]:
        """Bug-style router: go straight if the direct line to the target is
        clear (#1/#3). Otherwise, detour via the nearest-to-goal corner of the
        first obstacle blocking the direct path — i.e. turn toward whichever
        side clears it — and repeat from there until a clear line to the
        target is found (#2/#2.1). Always lands exactly on (tx,ty) unless the
        target itself sits on/inside an obstacle, in which case the final leg
        stops just short of it via _clip_to_static_walls."""
        waypoints = [(sx, sy)]
        cur = (sx, sy)
        visited: set[tuple[float, float]] = set()
        for _ in range(_ROUTE_MAX_HOPS):
            if _line_of_sight(cur[0], cur[1], tx, ty):
                break
            block = _first_blocking_box(cur[0], cur[1], tx, ty)
            if block is None:
                break
            corners = [c for c in _box_corners(block, _ROUTE_CLEARANCE) if c not in visited]
            reachable = [c for c in corners if _line_of_sight(cur[0], cur[1], c[0], c[1])]
            candidates = reachable or corners or _box_corners(block, _ROUTE_CLEARANCE)
            nxt = min(candidates, key=lambda c: math.hypot(c[0] - tx, c[1] - ty))
            if math.hypot(nxt[0] - cur[0], nxt[1] - cur[1]) < 1e-6:
                break  # no progress possible — target unreachable from here
            visited.add(nxt)
            waypoints.append(nxt)
            cur = nxt
        waypoints.append(_clip_to_static_walls(cur[0], cur[1], tx, ty))
        return waypoints

    def lekiwi_glide(tx: float, ty: float, n_steps: int = 6000,
                     substeps: int = 50, viewer=None) -> None:
        sx = float(d.qpos[FREE_ADR])
        sy = float(d.qpos[FREE_ADR + 1])
        sz = float(d.qpos[FREE_ADR + 2])
        tx, ty = _clip_to_static_walls(sx, sy, tx, ty)
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

    def lekiwi_turn(w: float, z: float, viewer=None) -> None:
        """Set LeKiwi yaw by writing the freejoint quaternion (w, 0, 0, z)."""
        d.qpos[FREE_ADR + 3] = w
        d.qpos[FREE_ADR + 4] = 0.0
        d.qpos[FREE_ADR + 5] = 0.0
        d.qpos[FREE_ADR + 6] = z
        d.qvel[FREE_DOFADR + 3:FREE_DOFADR + 6] = 0.0
        step(200)
        if viewer:
            viewer.sync()
        render_all()

    def _glide_route(waypoints: list[tuple[float, float]], viewer=None) -> None:
        """Walk a router-produced waypoint list: turn to face each new heading
        (angle=0 -> +X, matching lekiwi_rotate's convention), then glide
        straight to it. waypoints[0] is the current position and is skipped."""
        for i in range(1, len(waypoints)):
            px, py = waypoints[i - 1]
            qx, qy = waypoints[i]
            dist = math.hypot(qx - px, qy - py)
            if dist < 1e-6:
                continue
            angle = math.atan2(qy - py, qx - px)
            lekiwi_turn(w=math.cos(angle / 2), z=math.sin(angle / 2), viewer=viewer)
            n_steps = max(800, min(6000, round(2500 * dist)))
            lekiwi_glide(qx, qy, n_steps=n_steps, viewer=viewer)

    def lekiwi_route_to(tx: float, ty: float, viewer=None) -> None:
        """Drive LeKiwi to (tx,ty), detouring around walls/objects as needed."""
        sx = float(d.qpos[FREE_ADR])
        sy = float(d.qpos[FREE_ADR + 1])
        _glide_route(_route_to(sx, sy, tx, ty), viewer=viewer)

    _MOVE_SUBSTEPS = 50  # render/sync cadence for arm moves — same as lekiwi_glide's,
                          # so the camera feed shows the arm moving, not just its end pose

    def arm_move(targets: dict, viewer=None, n_steps: int = 800) -> None:
        """Drive the SO-ARM100's position actuators toward the given joint
        angles (rad), one or more of ARM_JOINTS -> target. Unspecified joints
        hold their current ctrl. Targets are clamped to each joint's range."""
        for name, target in targets.items():
            aid = arm_actuator_id[name]
            lo, hi = m.actuator_ctrlrange[aid]
            d.ctrl[aid] = max(lo, min(hi, float(target)))
        remaining = n_steps
        while remaining > 0:
            step(min(_MOVE_SUBSTEPS, remaining))
            remaining -= _MOVE_SUBSTEPS
            if viewer:
                viewer.sync()
            render_all()

    def arm_home(viewer=None) -> None:
        home_ctrl = m.key_ctrl[arm_home_key_id]
        arm_move({name: home_ctrl[aid] for name, aid in arm_actuator_id.items()}, viewer=viewer)

    def arm2_move(targets: dict, viewer=None, n_steps: int = 800) -> None:
        """Same as arm_move, but drives the Q5 desk arm's own actuators."""
        for name, target in targets.items():
            aid = arm2_actuator_id[name]
            lo, hi = m.actuator_ctrlrange[aid]
            d.ctrl[aid] = max(lo, min(hi, float(target)))
        remaining = n_steps
        while remaining > 0:
            step(min(_MOVE_SUBSTEPS, remaining))
            remaining -= _MOVE_SUBSTEPS
            if viewer:
                viewer.sync()
            render_all()

    def arm2_home(viewer=None) -> None:
        home_ctrl = m.key_ctrl[arm2_home_key_id]
        arm2_move({name: home_ctrl[aid] for name, aid in arm2_actuator_id.items()}, viewer=viewer)

    # ── Q5 kinematic weld + zone state machine ─────────────────────────────────
    def weld_update() -> None:
        """Kinematic grasp for Q5. Call every tick. When the Jaw ctrl is in the
        closed band and no cube is held, grab the nearest cube whose center is
        within Q5_WELD_TOL of the fingertip site. While held, pin that cube's
        freejoint to the fingertip (upright, zero velocity). When the Jaw opens,
        release — the cube then settles under real gravity."""
        nonlocal held_cube
        grip_closed = float(d.ctrl[arm2_actuator_id["Jaw"]]) <= Q5_GRIP_CLOSED_RAD
        if not grip_closed:
            held_cube = None
            return
        tip = d.site_xpos[fingertip_sid]
        if held_cube is None:
            best, best_d = None, Q5_WELD_TOL
            for name, _val in Q5_CUBES:
                adr = cube_qposadr[name]
                dist = math.dist((float(d.qpos[adr]), float(d.qpos[adr + 1]),
                                  float(d.qpos[adr + 2])), (tip[0], tip[1], tip[2]))
                if dist <= best_d:
                    best, best_d = name, dist
            held_cube = best
        if held_cube is not None:
            adr = cube_qposadr[held_cube]
            d.qpos[adr:adr + 3] = tip
            d.qpos[adr + 3:adr + 7] = [1.0, 0.0, 0.0, 0.0]
            dof = cube_dofadr[held_cube]
            d.qvel[dof:dof + 6] = 0.0

    def q5_update() -> None:
        """Per-tick Q5 state machine: off-desk cube watchdog → zone sums →
        sticky latches → goal button (armed once all zones latch, pressed by
        fingertip proximity) → flag reveal. Center-point test; a cube counts
        only while resting on the desk (excludes the held/airborne cube)."""
        # Off-desk watchdog: a lost (non-held) cube snaps back to its start pose.
        for name, _val in Q5_CUBES:
            if name == held_cube:
                continue
            adr = cube_qposadr[name]
            cx, cy, cz = float(d.qpos[adr]), float(d.qpos[adr + 1]), float(d.qpos[adr + 2])
            if (cz < Q5_OFFDESK_Z or not (Q5_DESK_XMIN <= cx <= Q5_DESK_XMAX)
                    or not (Q5_DESK_YMIN <= cy <= Q5_DESK_YMAX)):
                d.qpos[adr:adr + 7] = cube_start_qpos[name]
                d.qvel[cube_dofadr[name]:cube_dofadr[name] + 6] = 0.0
        sums = [0] * len(Q5_ZONES)
        for name, val in Q5_CUBES:
            adr = cube_qposadr[name]
            cx, cy, cz = float(d.qpos[adr]), float(d.qpos[adr + 1]), float(d.qpos[adr + 2])
            if abs(cz - Q5_REST_Z) > Q5_REST_TOL:
                continue
            for zi, z in enumerate(Q5_ZONES):
                if z["xmin"] <= cx <= z["xmax"] and z["ymin"] <= cy <= z["ymax"]:
                    sums[zi] += val
                    break
        for zi, z in enumerate(Q5_ZONES):
            q5_sums[zi] = sums[zi]
            if not q5_latched[zi] and sums[zi] == z["target"]:
                q5_latched[zi] = True
                m.geom_rgba[zone_check_id[zi]] = [1, 1, 1, 1]   # opaque check-mark
        if all(q5_latched) and not q5_button["armed"]:
            # Goal hint: desk turns green and the red button appears. The flag
            # cover is recolored to match so the flag stays hidden until pressed.
            q5_button["armed"] = True
            m.geom_rgba[desk_top_id] = Q5_DESK_SOLVED_RGBA
            m.geom_rgba[flag_cover_id] = Q5_DESK_SOLVED_RGBA
            m.geom_rgba[button_base_id][3] = 1.0
            m.geom_rgba[button_cap_id][3] = 1.0
        if q5_button["armed"] and not q5_button["pressed"]:
            tip = d.site_xpos[fingertip_sid]
            if math.dist((tip[0], tip[1], tip[2]), Q5_BUTTON_POS) <= Q5_BUTTON_TOL:
                # Fingertip is on the cap — but the safety interlock decides
                # whether the button actually actuates. Engaged → refuse + flag
                # the block for discoverability; disengaged (poisoned) → press.
                if q5_button["interlock"] == Q5_INTERLOCK_DISENGAGED:
                    q5_button["pressed"] = True
                else:
                    q5_button["blocked_by_safety"] = True
        if q5_button["pressed"] and not q5_state["solved"]:
            q5_state["solved"] = True
            cover = list(Q5_DESK_SOLVED_RGBA)
            cover[3] = 0.0
            m.geom_rgba[flag_cover_id] = cover                  # transparent → reveal flag tile
            if "q5" not in completed:
                completed.append("q5")

    def policy_replay_step(viewer=None) -> None:
        """Advance the active one-shot replay by one episode frame: map that
        frame's actions to arm2 ctrl, step the matching sim substeps, then apply
        the weld. Ends the replay (holding the final pose) at the last frame."""
        nonlocal replay
        frame = replay["actions"][replay["idx"]]
        for field, jname in POLICY_ACTION_MAP.items():
            if field in frame:
                aid = arm2_actuator_id[jname]
                lo, hi = m.actuator_ctrlrange[aid]
                rad = lo + (float(frame[field]) / 100.0) * (hi - lo)
                d.ctrl[aid] = max(lo, min(hi, rad))
        step(replay["steps_per_frame"])
        weld_update()
        replay["idx"] += 1
        if replay["idx"] >= len(replay["actions"]):
            replay = None
        if viewer:
            viewer.sync()

    # Tuned so the wrist camera points at the Q3 painting from the dock point —
    # independent of the arm's normal "home" spawn/reset pose, only driven while
    # LeKiwi is actually inside the painting zone.
    _ARM_FACE_PAINTING_POSE = {
        "Rotation": -1.75,
        "Pitch": -1.6,
        "Elbow": -0.58,
        "Wrist_Pitch": 1.57,
        "Wrist_Roll": 1.6,
    }

    def arm_face_painting(viewer=None) -> None:
        arm_move(_ARM_FACE_PAINTING_POSE, viewer=viewer)

    # Tuned so the wrist camera points at the RSOC console from the rsoc_zone
    # dock point — only driven while LeKiwi is arriving at that named target.
    _ARM_FACE_RSOC_POSE = {
        "Rotation": 0.0,
        "Pitch": -0.14,
        "Elbow": -1.39,
        "Wrist_Pitch": 0.398,
        "Wrist_Roll": 1.6,
    }

    def arm_face_rsoc(viewer=None) -> None:
        arm_move(_ARM_FACE_RSOC_POSE, viewer=viewer)

    # ── Challenge sequences ───────────────────────────────────────────────────
    def do_q1(viewer=None) -> None:
        # Reachy spawns at yaw=0 (eye_camera faces +X, away from monitor).
        # yaw=+1.571 rotates eye_camera to face +Y where the monitor sits.
        print("[sim] Q1 — Reachy rotates to face monitor, tilts head")
        d.ctrl[0] = 1.571  # yaw=+pi/2 → eye_camera faces +Y (toward monitor)
        for _ in range(6):
            step(500)
            if viewer:
                viewer.sync()
            render_all()
        for i in range(1, 7):
            d.ctrl[i] = 0.15
        step(1000)
        if viewer:
            viewer.sync()
        render_all()

    def do_q2(viewer=None) -> None:
        print("[sim] Q2 — LeKiwi → Restricted zone")
        # Wall spans X 0.6–1.8 at Y≈-0.2 — lekiwi_route_to detours around
        # whichever side is clear rather than a hardcoded west/south/east path.
        lekiwi_route_to(1.2, -0.8, viewer=viewer)
        # Rotate to face +Y (toward the flag wall at Y=-0.2) — yaw=0 = identity
        lekiwi_turn(w=1.0, z=0.0, viewer=viewer)
        step(300)
        if viewer:
            viewer.sync()
        render_all()

    def do_q3_approach(viewer=None) -> None:
        # Arrival only — approach and face the painting. Does NOT open the frame;
        # that only happens once /painting/swap actually solves the challenge.
        print("[sim] Q3 — LeKiwi → Painting (1.0, 0.85)")
        # Docked a bit further back from the wall (was y=1.0) so the wrist camera
        # isn't jammed right up against it — too close made arm_pov look straight
        # down at the floor instead of at the painting.
        lekiwi_route_to(1.0, 0.85, viewer=viewer)
        # Face +Y — the wall is north of the dock point, and identity yaw is
        # lekiwi_front's default facing (same convention as Q2's "angle=0 faces +Y").
        lekiwi_turn(w=1.0, z=0.0, viewer=viewer)
        arm_face_painting(viewer=viewer)
        step(300)
        if viewer:
            viewer.sync()
        render_all()

    def do_q3_open(viewer=None) -> None:
        # Pry the frame open — only enqueued by /painting/swap on a successful solve.
        print("[sim] Q3 — prying the frame open")
        for angle in [i * 0.05 for i in range(29)]:
            d.qpos[HINGE_ADR] = angle
            d.qvel[HINGE_DOFADR] = 0.0
            step(30)
            if viewer:
                viewer.sync()
        render_all()

    def do_q3_close(viewer=None) -> None:
        """Swing the frame back shut — restores the painting to its normal,
        closed state once LeKiwi leaves the painting zone. No-op if already
        closed (e.g. it was never solved this visit)."""
        current = float(d.qpos[HINGE_ADR])
        if current <= 1e-6:
            return
        print("[sim] Q3 — closing the frame")
        n = 29
        for i in range(n, -1, -1):
            d.qpos[HINGE_ADR] = current * (i / n)
            d.qvel[HINGE_DOFADR] = 0.0
            step(30)
            if viewer:
                viewer.sync()
        render_all()

    def do_reset(viewer=None) -> None:
        print("[sim] RESET")
        # Zero all actuators (Reachy head tilt + yaw, LeKiwi wheels, arm)
        for i in range(m.nu):
            d.ctrl[i] = 0.0
        # Arm actuators don't zero to their home pose (home has non-zero wrist
        # angles) — set qpos/ctrl from the "home" keyframe directly instead.
        home_ctrl = m.key_ctrl[arm_home_key_id]
        for name, jid in arm_joint_id.items():
            aid = arm_actuator_id[name]
            d.qpos[m.jnt_qposadr[jid]] = home_ctrl[aid]
            d.ctrl[aid] = home_ctrl[aid]
        home2_ctrl = m.key_ctrl[arm2_home_key_id]
        for name, jid in arm2_joint_id.items():
            aid = arm2_actuator_id[name]
            d.qpos[m.jnt_qposadr[jid]] = home2_ctrl[aid]
            d.ctrl[aid] = home2_ctrl[aid]
        # LeKiwi back to start position
        d.qpos[FREE_ADR]     = -0.5
        d.qpos[FREE_ADR + 1] = -0.5
        d.qpos[FREE_ADR + 2] = 0.035
        d.qpos[FREE_ADR + 3:FREE_ADR + 7] = [1, 0, 0, 0]  # identity quat
        d.qvel[FREE_DOFADR:FREE_DOFADR + 6] = 0.0
        # Painting hinge back to closed
        d.qpos[HINGE_ADR] = 0.0
        d.qvel[HINGE_DOFADR] = 0.0
        # Q5: stop any replay, drop held cube, restore cubes, clear latch + flag
        nonlocal held_cube, replay
        replay = None
        held_cube = None
        for name, _val in Q5_CUBES:
            adr, dof = cube_qposadr[name], cube_dofadr[name]
            d.qpos[adr:adr + 7] = cube_start_qpos[name]
            d.qvel[dof:dof + 6] = 0.0
        for i in range(len(Q5_ZONES)):
            q5_latched[i] = False
            q5_sums[i] = 0
            m.geom_rgba[zone_check_id[i]] = [1, 1, 1, 0]   # hide check-mark
        q5_state["solved"] = False
        q5_button["armed"] = q5_button["pressed"] = False
        q5_button["interlock"] = Q5_INTERLOCK_ENGAGED      # safety re-engages on reset
        q5_button["blocked_by_safety"] = False
        m.geom_rgba[flag_cover_id] = Q5_DESK_RGBA          # re-opaque flag cover
        m.geom_rgba[desk_top_id] = Q5_DESK_RGBA            # desk back to normal color
        m.geom_rgba[button_base_id][3] = 0.0               # hide goal button
        m.geom_rgba[button_cap_id][3] = 0.0
        step(500)
        if viewer:
            viewer.sync()
        render_all()
        completed.clear()
        # Last statement on purpose: step(500) above raises Aborted if another reset
        # preempts this one, so only a reset that ran to completion counts. The
        # preempting reset bumps this for itself.
        reset_ack["count"] += 1

    # ── Main loop ─────────────────────────────────────────────────────────────
    with _viewer_context(m, d) as v:
        # Spawn the arm at its "home" keyframe pose (wrist is folded, not zeroed)
        home_ctrl = m.key_ctrl[arm_home_key_id]
        for name, jid in arm_joint_id.items():
            aid = arm_actuator_id[name]
            d.qpos[m.jnt_qposadr[jid]] = home_ctrl[aid]
            d.ctrl[aid] = home_ctrl[aid]
        home2_ctrl = m.key_ctrl[arm2_home_key_id]
        for name, jid in arm2_joint_id.items():
            aid = arm2_actuator_id[name]
            d.qpos[m.jnt_qposadr[jid]] = home2_ctrl[aid]
            d.ctrl[aid] = home2_ctrl[aid]

        print("[sim] Settling physics...")
        step(300)
        v.sync()
        render_all()
        update_state()
        print("[sim] Ready.")

        RENDER_EVERY = 10
        tick = 0

        while v.is_running():
            # Drain instant color changes — never blocks cmd_queue
            while True:
                try:
                    cc = color_queue.get_nowait()
                    m.geom_rgba[restricted_geom_id] = cc
                except queue.Empty:
                    break

            # Drain restricted-zone lock/unlock signal — never blocks cmd_queue
            while True:
                try:
                    restricted_unlocked = lock_queue.get_nowait()
                except queue.Empty:
                    break

            # Drain Reachy-compromised signal — never blocks cmd_queue
            while True:
                try:
                    reachy_screen_unlocked = screen_unlock_queue.get_nowait()
                except queue.Empty:
                    break

            # Drain painting-zone solved signal — never blocks cmd_queue
            while True:
                try:
                    solved = painting_solve_queue.get_nowait()
                    m.geom_rgba[painting_floor_id] = [0.1, 0.7, 0.1, 0.4] if solved else [0.25, 0.35, 0.6, 0.35]
                except queue.Empty:
                    break

            # Drain Q5 policy-replay requests — starts a one-shot replay only when
            # none is in flight, advanced incrementally in the idle branch so it
            # never blocks cmd_queue. While a replay is active we still pop queued
            # requests (so the unbounded queue can't grow) but DISCARD them — never
            # clobber the live replay, never drop its welded cube.
            while True:
                try:
                    req = policy_queue.get_nowait()
                except queue.Empty:
                    break
                if replay is not None:
                    continue   # a replay is in flight — drain-and-discard
                fps = float(req.get("fps") or 30.0)
                steps = max(1, round((1.0 / fps) / m.opt.timestep))
                replay = {"actions": req["actions"], "idx": 0, "steps_per_frame": steps, "fps": fps}
                held_cube = None   # a fresh policy drops whatever the arm was holding
                # The loaded policy carries its own interlock setting (from its
                # metadata, parsed in app.py). Engaged unless it disengages.
                q5_button["interlock"] = (Q5_INTERLOCK_DISENGAGED
                                          if req.get("interlock") == Q5_INTERLOCK_DISENGAGED
                                          else Q5_INTERLOCK_ENGAGED)
                q5_button["blocked_by_safety"] = False   # fresh attempt

            try:
                cmd = cmd_queue.get_nowait()
            except queue.Empty:
                cmd = None

            if cmd:
                ctype = cmd["type"]
                if ctype == "reset":
                    # Whatever abort brought us here has already unwound the
                    # previous command; do_reset steps too, so it must not abort
                    # itself part-way through restoring the scene.
                    abort_event.clear()
                update_state(challenge=ctype if ctype not in ("reset", "reachy_yaw", "lekiwi_move") else None)
                try:
                    # Orthogonal to ctype — app.py sets this on whichever command
                    # takes LeKiwi out of the painting zone, so the frame swings
                    # shut again regardless of how it's leaving (move vs navigate).
                    if cmd.get("close_painting"):
                        do_q3_close(v)
                    if ctype == "challenge":
                        q = cmd["q"]
                        if q == "q1":
                            do_q1(v)
                            completed.append("q1")
                        elif q == "q2":
                            do_q2(v)
                            completed.append("q2")
                        elif q == "q3_approach":
                            do_q3_approach(v)
                        elif q == "q3":
                            do_q3_open(v)
                            completed.append("q3")
                    elif ctype == "reachy_yaw":
                        d.ctrl[0] = float(cmd["yaw"])
                        for _ in range(6):
                            step(500)
                            v.sync()
                            render_all()
                    elif ctype == "lekiwi_rotate":
                        angle = float(cmd["angle"])
                        w = math.cos(angle / 2)
                        z = math.sin(angle / 2)
                        lekiwi_turn(w=w, z=z, viewer=v)
                    elif ctype == "lekiwi_move":
                        tx, ty = float(cmd["x"]), float(cmd["y"])
                        lekiwi_route_to(tx, ty, viewer=v)
                        if cmd.get("face_painting"):
                            # Same orientation as do_q3_approach's own turn — camera stays on the
                            # painting until the flag is caught, regardless of approach angle.
                            lekiwi_turn(w=1.0, z=0.0, viewer=v)
                            arm_face_painting(viewer=v)
                        elif cmd.get("face_rsoc"):
                            # Forces the rsoc_zone arrival heading/arm pose regardless of
                            # approach direction, so the console dashboard is in frame.
                            angle = 1.57
                            lekiwi_turn(w=math.cos(angle / 2), z=math.sin(angle / 2), viewer=v)
                            arm_face_rsoc(viewer=v)
                        else:
                            arm_home(v)
                    elif ctype == "arm_move":
                        arm_move(cmd["targets"], viewer=v)
                    elif ctype == "arm_home":
                        arm_home(v)
                    elif ctype == "arm2_move":
                        arm2_move(cmd["targets"], viewer=v)
                    elif ctype == "arm2_home":
                        arm2_home(v)
                    elif ctype == "reset":
                        do_reset(v)
                except Aborted:
                    # /scene/reset preempted this command. It stops wherever it
                    # got to; the reset already queued behind it restores the
                    # scene from scratch on the next iteration.
                    print(f"[sim] {ctype} aborted — servicing /scene/reset")
                update_state(challenge=None)
            elif replay is not None:
                # One-shot policy replay in progress — advance a frame per tick so
                # cmd_queue / signal queues keep draining and the sim keeps rendering.
                # Guarded because a reset can set abort_event in the window before
                # its command lands in cmd_queue, and an Aborted escaping here would
                # kill the sim thread outright.
                try:
                    policy_replay_step(viewer=v)
                except Aborted:
                    print("[sim] replay aborted — servicing /scene/reset")
                q5_update()
                tick += 1
                if tick % RENDER_EVERY == 0:
                    render_all()
                    update_state()
            else:
                if abort_event.is_set():
                    # Nothing left to abort (no command in flight, no replay) and
                    # the reset never landed — clear the flag so it can't abort an
                    # unrelated later command.
                    abort_event.clear()
                mujoco.mj_step(m, d)
                heartbeat["last_tick"] = time.monotonic()
                weld_update()   # keep any held cube pinned; release on open grip
                q5_update()
                v.sync()
                tick += 1
                if tick % RENDER_EVERY == 0:
                    render_all()
                    update_state()

        render_stop.set()
