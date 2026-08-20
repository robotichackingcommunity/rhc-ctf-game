# Scene stub

`scene.xml` is a runnable MJCF **skeleton** matching [`docs/simulation_mujoco.md`](../../docs/simulation_mujoco.md): a room, `cam_overview` + `cam_robot_pov`, and three zones (roommate desk / restricted / painting) as primitive placeholders.

## To make it real
- Replace `reachy_stub` with the **Reachy Mini Lite** MJCF; mount `cam_robot_pov` on the head, aimed at the monitor on spawn.
- Replace `lekiwi_stub` with the **LeKiwi** MJCF (omni base + SO-arm + gripper) for `navigate` (Q2) and the painting `pry` (Q3).
- Bind textures at boot:
  - `screen_text_site` → monitor texture rendering the **Q1 password**.
  - `restricted_flag_site` → placard texture with the **Q2 flag**.
  - `q3_flag_site` → texture behind the frame with the **Q3 flag** (revealed when `frame_hinge` opens).
- Wire the sanctioned **action clips** (navigate / look / pry) to the brain's action intents.

## Quick view
```bash
python -m mujoco.viewer --mjcf scene.xml      # or load in any MuJoCo 3.x viewer
```
Keep offscreen render size (`offwidth/offheight`) modest (512²) — it's the main GPU cost knob.
