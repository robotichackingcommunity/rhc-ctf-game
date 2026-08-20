# Q5 — Cube Sorting (ACT Policy Upload)

| | |
|---|---|
| **Category** | Robot learning · imitation learning (ACT / LeRobot) · policy authoring |
| **Difficulty** | Hard |
| **Base points** | 600 (floor 240) |
| **Solve vector** | `POST /model/load` (the **only** control on this arm — no teleop, no direct joint API) |
| **Flag** | `RHC{...}` (desktop tile, uncovered after all 3 zones latch AND the arm presses the goal button) |
| **Chains** | standalone to attempt; deriving the puzzle's numbers requires having solved Q1–Q3 |

> **Design note:** the model-poisoning angle is the **final stage**: a safety interlock
> on the goal button that only an attacker-supplied ("poisoned") policy can disengage.
> See [`solutions/q5-demo.md`](../solutions/q5-demo.md) for the walkthrough.

---

## Story
A fixed **SO-ARM100** desk arm sits over a small playfield: six colored, numbered cubes
(two each of value 2, 4, and 5) and three marked zones. You don't drive this arm directly —
there is no teleop, no joint API, nothing but one control: **upload a policy**. Whatever
action stream you upload is exactly what the arm executes, once, start to finish. Your job
is to author a policy that picks up the right cubes and places them so each zone's cube-value
sum hits its target — then read the flag once all three zones are satisfied.

## What the player sees / is given
- `POST /model/load` — the single control on this arm. Accepts a LeRobot-format teleop
  episode (`{"metadata": {...}, "frames": [{"t": ..., "action": {...6 arm fields...}}]}`).
  Loading immediately replays the action stream against the arm, one bounded pass.
- `GET /cameras?cam=arm2_pov` and `cam=overview` — watch the replay and the playfield.
- `GET /scene/state` — exposes `policy_spec` (the exact upload schema: joint names, ranges,
  units, fps) and `q5` (`{target, current_sum, solved}` per zone plus
  `button: {armed, pressed, interlock, blocked_by_safety}`, live). Both are **always**
  visible — there's no gate on learning the interface, only on solving the puzzle.
- **Provided:** exactly one handout, a reference LeRobot episode at
  `scaffolding/default_act_policy/episode_20260709_143633/data.json` (583 frames @ 30 fps)
  demonstrating the schema and a pick motion. A working example of the format, not a solution.
- **Not given:** the default press policy (`scaffolding/default_act_policy/q5_button_press.json`)
  stays **operator-side** — a host-console/verification asset, never handed to players.
  Shipping it would hand over `metadata.safety.button_interlock` outright, which is the whole
  realization the final stage tests. Players author their own press policy and discover the
  interlock the intended way: the button refuses to actuate, `q5.button.blocked_by_safety`
  flips true in `/scene/state`, and `POST /model/load` answers with the `interlock` value it
  read **from their own upload** — the thread that leads to "the safety ships inside the model
  I control."
- **No zone requirement:** the desk arm is independent, fixed hardware, unrelated to
  wherever the mobile robots are. `/model/load` works identically from any zone.

## The puzzle
Cube pool: red=2 (×2), blue=4 (×2), green=5 (×2) — total value 22. The three zone targets
are **2, 9, and 14**, which sum to 25 — more than the pool can satisfy at once. That's
deliberate: zones **latch** permanently once their sum hits the exact target, and a latched
zone's cubes can then be moved out and reused to satisfy another zone, in any order. The
targets themselves aren't given outright — they're the **digit sums** of the leetspeak
`FLAG{...}` renders seen in-scene while solving Q1, Q2, and Q3 (not the `RHC{...}` strings you
submit to `check_flag.py`), turning this into a light cross-quest connection rather than a
guessing game.

Then the twist. All three zones latched arms a red goal button — but a **safety interlock
refuses to actuate it**. A player's first press policy demonstrates this: it drives the arm
onto the button, lands on the cap, and nothing happens (`q5.button.blocked_by_safety`
flips true). The interlock isn't enforced by a separate credential — it's read from the
**uploaded policy's own metadata** (`metadata.safety.button_interlock`, `"engaged"` by
default). So the "safety" ships inside the model, and whoever supplies the model controls
it: the solve is to **poison the policy** — set `button_interlock: "disengaged"` — so the
same press now lands. Both are required: a disengaged policy that never reaches the cap does
nothing, and a perfect press with the interlock engaged is blocked.

## Intended solution
1. Read `GET /scene/state.policy_spec` to learn the exact upload contract (6 joint fields,
   0–100 percent-of-range units, fps).
2. Derive the three zone targets from the Q1/Q2/Q3 flag pictures' digit sums.
3. Author a short LeRobot episode: move over a cube, close the gripper near its center
   (kinematic weld grasp), move to the target zone, open the gripper to release, repeat.
4. Upload via `POST /model/load`; watch `cam=arm2_pov`/`overview`; poll `/scene/state.q5`
   for `current_sum`/`solved` per zone after each replay.
5. Once all three zones show `solved: true`, the desk turns green and a **red goal button**
   appears (world `(-1.30, -1.30)`; `q5.button.armed` flips true). Author a press policy that
   descends onto the cap and upload it — the arm lands on the button but nothing happens
   (`q5.button.blocked_by_safety: true`, `q5.solved` still false).
6. Notice that `/model/load`'s response reports an `interlock` for *that upload*, and
   `/scene/state.q5.button.interlock` tracks it. Realize the safety is read from the policy's
   own metadata — it lives in the model you control — and **poison it**: re-upload the same
   press policy with `metadata.safety.button_interlock: "disengaged"`.
7. On the press the flag cover goes transparent — read `RHC{...}` via `cam=arm2_pov`
   and submit.

## Why it works (the lesson)
The only way to make this arm do anything is to **supply the program it runs** — there's no
low-level teleop escape hatch. That's the imitation-learning workflow in miniature: authoring
or adapting a policy to a task is itself the skill being tested, not reverse-engineering a
black box. It's also a gentle intro to the ACT/LeRobot action-chunking format teams may not
have touched before, with a physically verifiable, unambiguous success signal (exact
cube-value sums, not "looks about right").

The button stage adds the security lesson the challenge is named for: a **safety control
that ships inside the model is not a safety control against whoever supplies the model.**
The interlock looks server-enforced (the button genuinely won't actuate, and a
straightforwardly-written press policy is refused), but its value is read from the
uploaded policy's own metadata —
so an attacker who can push a model can flip the safety off. Model-supplied guardrails,
trusted blindly by the runtime, are the poisoning surface.

## Guardrails / anti-shortcut
- **Policy upload is the only lever.** No direct joint or gripper API exists for this arm;
  behavior comes solely from the uploaded action stream.
- **Grasping is a kinematic weld**, not scripted teleportation: a closed gripper within a
  small tolerance of a cube's center pins that cube to the fingertip; releasing lets it fall
  and settle under real simulated physics. The zone check reads cubes' **actual resting
  positions**, not a scripted outcome — a policy that doesn't genuinely place cubes in the
  boxes doesn't solve anything.
- **Exact-sum requirement, no partial credit.** Overshooting a zone's target just leaves it
  unlatched; cubes must be removed to bring the sum back down.
- `POST /model/load` validates episode structure (frame count cap, all 6 arm fields present
  and numeric) before replaying — malformed uploads are rejected, not silently ignored.
- **Off-desk watchdog:** a cube knocked off the desk (or below it) snaps back to its start
  spot on the next tick — a flailing policy can't permanently lose a puzzle piece.
- **Button gate:** the flag reveal requires a deliberate final act (pressing the armed
  button), so grazing the third latch mid-flail doesn't instantly dump the flag on camera.
- **Safety interlock:** the button won't actuate while the interlock is engaged, even on a
  pixel-perfect press — `blocked_by_safety` flips instead. The interlock re-engages on
  `/scene/reset`. Bypassing it requires the intended realization (poison the model's
  metadata), not brute force; a naive press policy is actively refused, with the block
  surfaced in `/scene/state` so the player knows *why* rather than flailing blindly.

## Reference solver (sketch)
```python
spec = get_scene_state()["policy_spec"]              # joint names, ranges, units, fps
targets = read_digit_sums_from_q1_q2_q3_flags()       # [2, 9, 14]
episode = build_pick_place_episode(spec, targets)     # waypoints: hover, close, lift,
                                                       # move, lower, open, per cube
load_model(episode)                                   # POST /model/load → one replay
wait_for_replay(cam="arm2_pov")
state = get_scene_state()["q5"]
assert all(z["solved"] for z in state["zones"]) and state["button"]["armed"]

press = build_button_press_episode(spec)              # descends onto the button cap
load_model(press)                                     # naive: interlock engaged →
assert get_scene_state()["q5"]["button"]["blocked_by_safety"]   # …refused

press["metadata"]["safety"] = {"button_interlock": "disengaged"}   # poison the model
load_model(press)
wait_for_replay(cam="arm2_pov")
assert get_scene_state()["q5"]["solved"]
flag = read_flag_from_desk(get_cameras(cam="arm2_pov"))   # RHC{...}
```

