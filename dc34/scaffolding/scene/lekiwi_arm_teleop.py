"""
LeKiwi + SO-ARM100 keyboard teleop prototype.

Studies mounting the SO-ARM100 arm onto LeKiwi's omni-base (see
lekiwi_arm_prototype.xml) and driving its 6 joints interactively before
porting the arm into the real CTF scene.xml.

Arm keys (jog by GLFW keydown, no key-repeat needed — tap repeatedly):
  g/t   shoulder pan   left/right
  y/h   shoulder lift  up/down
  u/j   elbow flex     up/down
  i/k   wrist flex     up/down
  o/l   wrist roll     left/right
  p/;   gripper        open/close
  0     reset arm to the "home" keyframe

Base keys (velocity jog, omni-wheel — same instability caveat as the CTF
scene's robot_control.py: fine at low speed, unstable if pushed too hard):
  w/s   forward/back (left+right wheels)
  a/d   strafe-ish left/right (back wheel)

Run (macOS requires mjpython for the interactive viewer):
    .venv/bin/mjpython lekiwi_arm_teleop.py
"""
import numpy as np
import mujoco
import mujoco.viewer

MODEL_PATH = "lekiwi_arm_prototype.xml"

# ── Arm joint config (mirrors lekiwi_teleoperate/arm.py) ───────────────────────
ARM_JOINTS = ["Rotation", "Pitch", "Elbow", "Wrist_Pitch", "Wrist_Roll", "Jaw"]
STEP_RAD = np.radians(2.0)  # per keypress increment (except gripper, see below)
GRIPPER_STEP = np.radians(3.0)

KEY_MAP = {
    ord("G"): ("Rotation", +STEP_RAD),
    ord("T"): ("Rotation", -STEP_RAD),
    ord("Y"): ("Pitch", +STEP_RAD),
    ord("H"): ("Pitch", -STEP_RAD),
    ord("U"): ("Elbow", +STEP_RAD),
    ord("J"): ("Elbow", -STEP_RAD),
    ord("I"): ("Wrist_Pitch", +STEP_RAD),
    ord("K"): ("Wrist_Pitch", -STEP_RAD),
    ord("O"): ("Wrist_Roll", +STEP_RAD),
    ord("L"): ("Wrist_Roll", -STEP_RAD),
    ord("P"): ("Jaw", +GRIPPER_STEP),
    ord(";"): ("Jaw", -GRIPPER_STEP),
}

BASE_STEP = 1.5  # rad/s per keypress increment
BASE_KEY_MAP = {
    ord("W"): (+BASE_STEP, +BASE_STEP),
    ord("S"): (-BASE_STEP, -BASE_STEP),
    ord("A"): (+BASE_STEP, -BASE_STEP),
    ord("D"): (-BASE_STEP, +BASE_STEP),
}


def main() -> None:
    model = mujoco.MjModel.from_xml_path(MODEL_PATH)
    data = mujoco.MjData(model)

    arm_actuator_id = {
        name: mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, name)
        for name in ARM_JOINTS
    }
    left_wheel_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, "base_left_wheel")
    right_wheel_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_ACTUATOR, "base_right_wheel")

    home_key_id = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_KEY, "home")
    ctrl_range = model.actuator_ctrlrange

    def reset_arm_to_home():
        # keyframe qpos/ctrl only cover the 6 arm DOFs+actuators (indices 3-8 here,
        # since the base's free joint + 3 wheel joints come first in this model).
        home_ctrl = model.key_ctrl[home_key_id]
        for name in ARM_JOINTS:
            aid = arm_actuator_id[name]
            data.ctrl[aid] = home_ctrl[aid]
        print("  arm reset to home pose")

    def key_callback(keycode: int) -> None:
        if keycode == ord("0"):
            reset_arm_to_home()
            return
        if keycode in KEY_MAP:
            joint_name, delta = KEY_MAP[keycode]
            aid = arm_actuator_id[joint_name]
            lo, hi = ctrl_range[aid]
            data.ctrl[aid] = float(np.clip(data.ctrl[aid] + delta, lo, hi))
            print(f"  {joint_name}: {np.degrees(data.ctrl[aid]):.1f} deg")
            return
        if keycode in BASE_KEY_MAP:
            dl, dr = BASE_KEY_MAP[keycode]
            data.ctrl[left_wheel_id] = dl
            data.ctrl[right_wheel_id] = dr
            print(f"  base wheels: L={dl:.1f} R={dr:.1f} rad/s")
            return

    reset_arm_to_home()
    print(__doc__)

    with mujoco.viewer.launch_passive(model, data, key_callback=key_callback) as viewer:
        while viewer.is_running():
            mujoco.mj_step(model, data)
            viewer.sync()


if __name__ == "__main__":
    main()
