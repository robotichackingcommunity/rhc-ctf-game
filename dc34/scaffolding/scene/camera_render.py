"""
CTF Camera Rendering — captures frames from all robot cameras and saves PNGs.
Run from ctf_scene/:
    mjpython camera_render.py

Images are saved to ctf_scene/frames/ and opened in Preview on first capture.
Press S in the terminal (or wait for auto-save every 5 seconds of sim time).
"""
import os
import time
import subprocess
import mujoco
import mujoco.viewer
from PIL import Image

# ── Setup ────────────────────────────────────────────────────────────────────
m = mujoco.MjModel.from_xml_path("scene.xml")
d = mujoco.MjData(m)

os.makedirs("frames", exist_ok=True)

CAMERAS = ["eye_camera", "lekiwi_front", "cam_overview"]
renderers = {cam: mujoco.Renderer(m, height=480, width=640) for cam in CAMERAS}

print("Cameras:", CAMERAS)
print("Frames saved to: ctf_scene/frames/")
print("Auto-saves every 5 seconds of sim time. Close viewer to quit.\n")

def capture_and_save(step):
    paths = []
    for name, renderer in renderers.items():
        renderer.update_scene(d, camera=name)
        img = renderer.render()          # (H, W, 3) uint8 numpy array
        path = f"frames/{name}_step{step:06d}.png"
        Image.fromarray(img).save(path)
        paths.append(os.path.abspath(path))
        print(f"  saved: {path}  brightness={img.mean():.1f}")
    return paths

# ── Main loop ────────────────────────────────────────────────────────────────
step = 0
first_save = True
SAVE_EVERY = 2500   # steps (~5 sim seconds at dt=0.002)

with mujoco.viewer.launch_passive(m, d) as v:
    while v.is_running():
        mujoco.mj_step(m, d)
        v.sync()

        if step % SAVE_EVERY == 0:
            print(f"\n[step {step}] Capturing all cameras...")
            paths = capture_and_save(step)

            # Open in macOS Preview on first save so you can see them
            if first_save:
                subprocess.Popen(["open", "-a", "Preview"] + paths)
                first_save = False

        step += 1
        time.sleep(0.002)
