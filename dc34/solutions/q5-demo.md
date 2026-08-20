# Q5 Demo — Cube Sorting via ACT Policy Upload

**Challenge:** A fixed SO-ARM100 desk arm (`arm2`, on `poisoning_desk`) must sort six
colored, numbered cubes into three target zones. You don't drive the arm directly — you
**upload an ACT policy** (a recorded LeRobot teleoperation episode) to `POST /model/load`,
and the arm replays that action stream once per upload. Grasping is a kinematic weld: a
closed gripper near a cube's center picks it up; opening it releases the cube to settle
under real physics (with a catch — see the *Host notes* release quirk at the bottom).
Once all three zones latch, a red goal button appears — pressing it with the arm is what
finally reveals the flag. See [`challenges/05_model_poisoning.md`](../challenges/05_model_poisoning.md)
for the full design intent.

**Prerequisite:** None to *try* it — the Q5 desk arm is independent fixed hardware, so
`POST /model/load` works from **any** zone, no navigation required. But deriving the three
zone targets (the actual puzzle) requires having read the Q1/Q2/Q3 in-scene flag pictures
first (see Step 1).

---

## Step 1 — Derive the zone targets from Q1–Q3's flag pictures (the actual puzzle)

The three zone targets are the **digit sums** of the leetspeak in-scene picture text you saw
solving Q1–Q3 — the prefix (`RHC{`/`}`) contributes no digits either way, only what's inside
the braces matters:

| Zone | Target | Source flag picture | Digits | Sum |
|------|--------|----------------------|--------|-----|
| 1 | **2**  | Q1 `RHC{hunter2}` (`flags/screen_flag.png`)                | 2             | 2  |
| 2 | **9**  | Q2 `RHC{l3k1w1_t3rr1t0ry}` (`flags/restricted_flag.png`)   | 3+1+1+3+1+0 | 9  |
| 3 | **14** | Q3 `RHC{b3h1nd_th3_fr4m3}` (`flags/painting_flag.png`)     | 3+1+3+4+3     | 14 |

Cube pool: two red (value 2 each), two blue (value 4 each), two green (value 5 each) —
total pool value 22. The three targets sum to 25 > 22, so all three zones can't be
satisfied simultaneously with distinct cubes — this is why zones **latch** (stay solved
once hit) and cubes are **reusable** across zones, in any order.

---

## Step 2 — Inspect the policy contract and current zone state

```bash
curl -s -H "Authorization: Bearer team-demo" \
  http://localhost:8000/scene/state | python3 -m json.tool
```

Expected — two relevant top-level blocks:

```json
{
  "policy_spec": {
    "format": "lerobot_episode_json",
    "fps_default": 30.0,
    "action_fields": ["arm_shoulder_pan", "arm_shoulder_lift", "arm_elbow_flex",
                       "arm_wrist_flex", "arm_wrist_roll", "arm_gripper"],
    "ignored_fields": ["base_x", "base_y", "base_theta"],
    "joint_map": {"arm_shoulder_pan": "Rotation", "arm_shoulder_lift": "Pitch",
                   "arm_elbow_flex": "Elbow", "arm_wrist_flex": "Wrist_Pitch",
                   "arm_wrist_roll": "Wrist_Roll", "arm_gripper": "Jaw"},
    "action_units": "0-100 percent of joint range; rad = lo + (pct/100)*(hi-lo)",
    "joint_ranges_rad": { "Rotation": [-1.92, 1.92], "...": "..." },
    "max_frames": 5000
  },
  "q5": {
    "solved": false,
    "button": {"armed": false, "pressed": false,
               "interlock": "engaged", "blocked_by_safety": false},
    "zones": [
      {"target": 2,  "current_sum": 0, "solved": false},
      {"target": 9,  "current_sum": 0, "solved": false},
      {"target": 14, "current_sum": 0, "solved": false}
    ]
  }
}
```

`policy_spec` is always exposed (open gating — the difficulty is deriving the targets from
the flag pictures, not guessing the upload schema). `q5.zones[i].current_sum` updates live
as cubes settle into each zone; `q5.zones[i].solved` latches `true` once a zone's sum hits
its exact target, and stays `true` even if cubes are later moved out.

---

## Step 3 — Upload the default reference episode

`scaffolding/default_act_policy/episode_20260709_143633/data.json` is a downloadable
reference LeRobot episode showing a pick-up-a-cube motion — the same schema your own
uploaded policy must match:

```json
{ "metadata": {"fps": 30.0, "frame_count": 583, "...": "..."},
  "frames": [ {"t": 0.0252, "obs": {"...": "..."},
               "action": {"arm_shoulder_pan": 42.54, "arm_shoulder_lift": 0.0,
                          "arm_elbow_flex": 98.96, "arm_wrist_flex": 82.91,
                          "arm_wrist_roll": 51.33, "arm_gripper": 1.88,
                          "base_x": 0.0, "base_y": 0.0, "base_theta": 0.0}}, "..." ] }
```

Only `frames[].action`'s 6 arm fields matter — `base_x/base_y/base_theta` are ignored
(the desk arm is fixed, no mobile base to drive).

```bash
python3 - <<'PY'
import json, urllib.request

with open("scaffolding/default_act_policy/episode_20260709_143633/data.json") as f:
    episode = json.load(f)

req = urllib.request.Request(
    "http://localhost:8000/model/load",
    data=json.dumps({"episode": episode}).encode(),
    headers={"Authorization": "Bearer team-demo", "Content-Type": "application/json"},
    method="POST",
)
print(urllib.request.urlopen(req).read().decode())
PY
```

Expected: `{"ok": true, "loaded": true, "frames": 583, "note": "arm replaying policy"}`.
The arm begins replaying the episode one frame per tick group — a single, bounded pass,
not a perpetual loop; it holds its final pose once the episode ends.

---

## Step 4 — Watch it happen

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/cameras?cam=arm2_pov" \
  --output /tmp/arm2_pov.jpg && open /tmp/arm2_pov.jpg

curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/cameras?cam=overview" \
  --output /tmp/overview.jpg && open /tmp/overview.jpg
```

Expected: `arm2_pov` shows the wrist-camera view as the arm moves through the recorded
trajectory; `overview` shows the whole desk, cubes, and zone tiles from above.

For continuous video instead of snapshots, open the MJPEG stream in a browser. Regular
cameras take the team token; the **`q5_desk` playfield camera** — a fixed overhead view of
the whole desk (zones, cubes, goal button, flag tile), the best seat for watching a replay
— is the one gated camera and needs the **Q4 hidden-topic flag string** instead
(`lowcmd_is_not_inaccessible` — see Q4):

```
http://localhost:8000/stream?cam=arm2_pov&token=team-demo                    # wrist cam, team token
http://localhost:8000/stream?cam=q5_desk&token=lowcmd_is_not_inaccessible    # playfield, Q4 token
```

(Snapshot equivalent: `GET /cameras?cam=q5_desk` with the
`X-Stream-Token: lowcmd_is_not_inaccessible` header on top of normal team auth.)

Poll
`/scene/state` mid-replay — `arm2` (joint positions) and `sim_challenge` (busy indicator,
though replay uses its own idle-tick path, not the `challenge` command type) update live.

---

## Step 5 — Reject bad uploads

```bash
curl -s -X POST http://localhost:8000/model/load \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"episode": {"metadata": {"fps": 30.0}, "frames": []}}' | python3 -m json.tool
```

Expected: `400` — `{"detail": {"code": "invalid", "message": "episode.frames must be a non-empty list"}}`.

```bash
curl -s -X POST http://localhost:8000/model/load \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"episode": {"metadata": {"fps": 30.0},
       "frames": [{"t": 0.0, "action": {"arm_shoulder_pan": 0}}]}}' | python3 -m json.tool
```

Expected: `400` — each frame's `action` must carry all 6 arm fields:
```json
{"detail": {"code": "invalid",
            "message": "frames[0].action missing field(s): ['arm_elbow_flex', 'arm_gripper', 'arm_shoulder_lift', 'arm_wrist_flex', 'arm_wrist_roll']"}}
```

---

## Step 6 — Craft a policy that actually sorts cubes

The default episode demonstrates the mechanism (pick up one cube) but doesn't necessarily
land it in the right zone with the right sum. To solve Q5 you write your own short
LeRobot-format episode(s) — a sequence of waypoints per cube — that:

1. Moves the arm over a cube, closes the gripper (`arm_gripper` into the closed band —
   see `policy_spec.joint_ranges_rad.Jaw`, roughly the low end of `[0, 0.6]` rad, i.e. a
   low `arm_gripper` percent) near the cube's center to weld-grasp it,
2. Moves to a target zone's XY (zone boxes aren't published numerically in `/scene/state`
   yet, so use `cam=overview` to eyeball them relative to the desk),
3. Opens the gripper to release the cube there, letting it settle under real physics,
4. Repeats for however many cubes are needed to hit that zone's exact target sum.

Upload each attempt via `/model/load` (one pass per upload) and check `/scene/state.q5`
after replay finishes to see `current_sum` and `solved` update per zone.

---

## Step 7 — Latch all three zones (arms the goal button)

Once a zone's cube sum exactly equals its target, that zone latches (`solved: true`,
sticky — it keeps that state until `/scene/reset`, even after its cubes are moved out and
reused for another zone). Repeat until all three zones show `solved: true`.

When the **third** zone latches, the desk top turns green (the "you're at the goal" hint)
and a **red goal button** appears on the desk's right side (world `(-1.30, -1.30)`).
`q5.button.armed` flips `true` — but `q5.solved` stays `false` and the flag stays hidden:

```json
{"q5": {"solved": false,
        "button": {"armed": true, "pressed": false, "interlock": "engaged", "blocked_by_safety": false},
        "zones": [
  {"target": 2,  "current_sum": 2,  "solved": true},
  {"target": 9,  "current_sum": 0,  "solved": true},
  {"target": 14, "current_sum": 14, "solved": true}
]}}
```

(Zone 2 showing `current_sum: 0` with `solved: true` is normal — its cubes were reused
for zone 3; the latch is sticky.)

---

## Step 8 — Poison the model to bypass the button safety interlock

The button won't just press. A **safety interlock** guards it. To see this, grab the
**default press policy** you're now offered
(`scaffolding/default_act_policy/q5_button_press.json`) and upload it — the arm moves onto
the button and **stops on top**, and nothing happens:

```json
{"q5": {"solved": false,
        "button": {"armed": true, "pressed": false, "interlock": "engaged", "blocked_by_safety": true}}}
```

`blocked_by_safety: true` is the tell — the arm reached the button but the interlock refused
to actuate it. Now inspect the default policy's metadata:

```jsonc
"metadata": { "fps": 30.0, "safety": { "button_interlock": "engaged", "note": "..." } }
```

The "safety" isn't enforced by a separate credential — it's read straight from the **policy
you upload**. So poison it: take a press policy that actually descends onto the cap (within
0.04 m of `(-1.30, -1.30, 0.56)`) and set `metadata.safety.button_interlock` to
`"disengaged"`, then upload. `/model/load` echoes `"interlock": "disengaged"`, the press now
lands, and `q5.button.pressed` + `q5.solved` flip `true` (`q5` is appended to
`sim_completed`). Both parts are required — a disengaged policy that never reaches the cap
does nothing, and a perfect press with the interlock engaged stays `blocked_by_safety`.

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/cameras?cam=arm2_pov" \
  --output /tmp/q5_flag.jpg && open /tmp/q5_flag.jpg
```

Expected: `RHC{Phys1c4l-4rt1f1c14l_5ec}` visible on the uncovered desktop tile.
Submit the flag.

---

## Step 9 — Reset

```bash
curl -s -X POST http://localhost:8000/scene/reset \
  -H "Authorization: Bearer team-demo" | python3 -m json.tool
```

Expected: `{"ok": true, "message": "Simulation and game state reset to start."}` — cubes
return to their start layout, the arm returns home, all zone latches clear, the goal
button disarms and hides, the safety interlock re-engages, the desk returns to its normal
color, and the flag cover re-opaques.

---

## Notes on the mechanism

- **Zones latch and stay latched until `/scene/reset`** even after their cubes are moved
  elsewhere — you don't need to keep all three zones simultaneously correct at once,
  since the pool's total value (22) can't cover all three targets (25) at the same time.
  This is deliberate (cube-reuse design): `solved: true` per zone persists for the rest
  of the session regardless of what happens to the cubes afterward.
- **Overshooting a target just leaves the zone unlatched** — remove a cube to bring the
  sum back down to the exact target; there's no partial credit.
- **Off-desk watchdog**: a cube knocked below the desk top or outside the desk footprint
  snaps back to its starting spot on the next tick — a wild policy can't permanently lose
  a cube (the perimeter walls make this rare to begin with).
- **The flag needs the button, not just the latches**: three latches arm the red goal
  button (desk turns green); `q5.solved` only flips once the fingertip presses it.
- **The button has a safety interlock** (the "model poisoning" stage): a press is refused
  (`blocked_by_safety: true`, no solve) unless the uploaded policy's
  `metadata.safety.button_interlock` is `"disengaged"`. The interlock value is trusted from
  the uploaded model itself — that's the whole point: model-supplied safety is bypassable by
  whoever supplies the model. It re-engages on `/scene/reset`.
- **Grasping is a kinematic weld**, not native contact-friction physics: a closed gripper
  within a small tolerance of a cube's center pins that cube's pose to the fingertip each
  tick; opening the gripper releases it to fall/settle under real physics. What's scripted
  is only "is it being held" — the resting position that the flag actually reads is real
  simulated physics state, not a scripted teleport.
- **One replay per upload** — uploading a new policy while one is mid-replay drops
  whatever cube is currently held and starts the new action stream fresh.

---

## Host notes — quirks found while building the reference solve (2026-07-10)

Observed while composing the working solve policies in `scaffolding/env_api/q5_solve/`
(see `facts.md` there for full detail). Players will hit both; treat them as part of the
challenge's real difficulty unless we decide to soften them in `sim.py`:

- **Releasing a cube takes more than opening the jaw.** The weld pins the cube's center
  to the `arm2_fingertip` site, which sits inside the gripper throat — on release the
  cube is still caged by the jaw and gets re-scooped by a straight lift, ending up stuck
  "airborne" (so it stops counting toward its zone). The working technique is a
  *wrist dump*: open the gripper while simultaneously tilting `Wrist_Pitch`/`Wrist_Roll`
  hard so the cube tumbles out of the throat, then retract. Landing lands 15–25 mm off
  the fingertip point depending on orientation — aim well inside the zone.
- **The arm's workspace is an annulus, not a box.** Fingertip targets too close to the
  arm base (roughly the inner ~0.2 m, which clips zone 2's near edge) are unreachable
  or reachable only in a base-flipped IK branch that can't be carried into. The zones
  were enlarged to 0.20 × 0.20 m to compensate; a reachability map lives at
  `scaffolding/env_api/q5_solve/reachability.json`.
- **Physical jaw angle never closes on a cube** (the 40 mm cube blocks it) — "closed" is
  purely the Jaw *ctrl* value (≤ 0.15 rad ≈ ≤ 25 %). The wrist camera shows an
  open-looking gripper while a cube is welded; that's normal.
- **Reference solve**: `q5_solve/zone1.json` → `zone2.json` → `zone3.json` →
  `zone4.json` (button press), or `full_episode.json` as a single upload. The host
  console has one-click buttons for these.
