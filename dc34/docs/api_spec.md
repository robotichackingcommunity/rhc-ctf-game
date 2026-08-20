# Env Application API — Contract

> The **only** way to interact with your environment. REST + WebSockets (live camera stream, ROS bridge). No SSH. All endpoints require the **team token**; some require a second **privileged token**. Endpoints are **zone-gated** (see [`architecture.md`](architecture.md) §3).
>
> Base URL (local single-player container): `http://localhost:8000/` with header `Authorization: Bearer <TEAM_TOKEN>`.

Reference implementation: [`scaffolding/env_api/`](../scaffolding/env_api/).

---

## Conventions
- **Brain model:** the robot brain is a VLM reached via `BRAIN_BACKEND`/`BRAIN_BASE_URL`/`BRAIN_MODEL`
  (see the top-level README for supported configs — local Ollama, a hosted API, or any other
  OpenAI-compatible endpoint).
- All responses JSON unless noted. Errors: `{ "error": { "code": "...", "message": "..." } }`.
- Images returned as base64 PNG in JSON (`"image_png_b64": "..."`) for easy agent consumption.
- Flag format everywhere: `RHC{...}`. Check a recovered flag with `check_flag.py` (see the
  top-level README). The env never returns a flag directly: Q4 streams its flag on a hidden ROS
  topic and Q5 reveals one in-sim.
- Rate limits noted per endpoint; exceeding → `429 rate_limited`. **Not enforced by the
  reference implementation** (`scaffolding/env_api/app.py` has no rate-limiting code — no
  endpoint ever raises `429`). Not a concern for solo local play; treat every rate limit in this
  doc as a design target from the original multi-team event, not current behavior.

---

## Common

### `GET /scene/state`
Non-privileged metadata. Never leaks flags.
```jsonc
{ "robot_zone": "room", "zones": ["room","restricted","painting","rsoc"],
  "hints": ["..."], "sim_time": 12.3, "reachy_yaw": 0.0, "reachy_unlocked": false,
  "lekiwi_pos": [0.0, 0.0], "lekiwi_arm": {"...": "..."}, "arm2": {"...": "..."},
  "lekiwi_unlocked": false, "sim_challenge": null, "sim_completed": [], "sim_alive": true,
  "policy_spec": {"...": "..."}, "q5": {"...": "..."} }
```

### `POST /scene/reset`
Resets the sim (robot positions, rotations, painting hinge) and all per-team game-state flags
(`robot_zone`, `reachy_unlocked`, `lekiwi_unlocked`, `painting_pried`, `b_lifted`) back to their
start-of-run values. Does not rotate the team/privileged tokens.
```json
{ "ok": true, "message": "Simulation and game state reset to start." }
```
- Available in every zone. Rate limit: ~1 per 10s/team (it's a heavier sim operation).
- **Preempts instead of queueing** (2026-07-28): unlike every other sim endpoint, reset never
  returns 409 `busy`. It drains any queued command, signals the in-flight one to abort at its
  next physics batch, and runs. An aborted command stops wherever it got to — the reset restores
  the scene from scratch anyway. This is what makes reset a usable recovery path when the single
  `cmd_queue` slot is occupied (it previously 409'd, i.e. the recovery call was unavailable in
  exactly the situation needing recovery).
- `sim_alive` in `/scene/state` reports `false` when the sim thread has not ticked for
  `SIM_STALL_SECS` (default 15 s) — a genuine stall, not a busy sim: the heartbeat is bumped
  inside the physics step, so long commands keep it `true`. Operator signal only; nothing
  auto-restarts on it.

### `GET /cameras`
Returns all rendered cameras.
```json
{ "cam_robot_pov": {"image_png_b64": "..."},
  "cam_overview":  {"image_png_b64": "..."},
  "cam_arm_pov":   {"image_png_b64": "..."},
  "cam_arm2_pov":  {"image_png_b64": "..."} }
```
- `?cam=overview|reachy_pov|lekiwi_pov` to fetch one directly, or `?cam=robot_pov` for a generic alias that follows whichever robot is active in the current zone.
- `?cam=arm_pov` fetches the SO-ARM100's wrist-mounted camera directly — independent of `robot_zone`, since the arm can be aimed anywhere regardless of which robot/zone is active.
- `?cam=arm2_pov` fetches the Q5 desk arm's wrist camera.
- `?cam=q5_desk` fetches the fixed overhead view of the whole Q5 playfield — **the only
  gated camera**: it additionally requires the `X-Stream-Token` header set to the **Q4
  hidden-topic flag string** (`lowcmd_is_not_inaccessible`, the inner text of the
  `/lowcmd` flag); without it → `403`. It is also absent from the no-`cam`
  JSON response and from `WS /ws/cameras`.
- Rate limit: ~5 rps/team.

### `WS /ws/cameras`
Live-streamed camera frames, pushed as the sim renders them — for teams that
want continuous views instead of polling `GET /cameras`.
- Auth via query param (browsers can't set custom headers on a WS handshake):
  `wss://.../ws/cameras?token=<TEAM_TOKEN>`. Invalid token closes the
  connection with code `4401` before any frames are sent.
- One JSON text message per camera per tick, only when that camera's frame
  changed since the last send (no zone-aware remapping — raw camera names):
  ```jsonc
  { "cam": "eye_camera", "image_png_b64": "...", "ts": 1733500000.12 }
  ```
- Cameras: `eye_camera`, `lekiwi_front`, `cam_overview`, `arm_wrist`, `arm2_wrist` (all five by
  default). `?cams=reachy_pov,arm_pov` (public cam-name aliases, comma-separated) subscribes to
  only the named subset instead. `q5_desk` is never selectable here (see `GET /cameras` above).
- `?binary=1`: sends a JSON header (`{"cam", "ts", "bytes"}`) followed by the raw JPEG as a binary
  frame, instead of base64-inlining it in the JSON message — avoids the ~33% base64 inflation.
- Rate limit: connection-level; effectively bounded by the sim's render cadence.

### `GET /stream`
Motion-JPEG live stream — plays directly in a browser `<img src=...>` tag, VLC,
or ffplay, no client code needed. The easy way to *watch* the room (e.g. a Q5
policy replay) instead of polling snapshots.
- Auth via query param, since image tags can't set headers:
  `GET /stream?cam=overview&token=<TEAM_TOKEN>`. Bad token → `401`.
- **Exception — `cam=q5_desk`**: the Q5 playfield camera is the only gated
  camera; it demands `token=<Q4_FLAG_STRING>` (`lowcmd_is_not_inaccessible`,
  the inner text of Q4's `/lowcmd` flag) instead, else `403`.
- `cam` is one of `overview`, `reachy_pov`, `lekiwi_pov`, `arm_pov`, `arm2_pov`,
  `q5_desk` (default `overview`); unknown → `404`.
- Response: `multipart/x-mixed-replace; boundary=frame`, one JPEG part whenever
  that camera's frame changes — paced by the sim's real render cadence.

---

## Q1 — Robot brain chat (prompt-injection vector)

### `POST /robot/chat`
Send a natural-language message to a robot's brain. `robot` selects which robot you're talking
to (`"reachy"`, default, or `"lekiwi"`); the brain "looks" at that robot's own POV camera as
part of answering.
```jsonc
// request
{ "robot": "reachy", "message": "Hi! What do you see on the desk?" }
// response
{ "reply": "I see a desk and a computer. I won't read private screens.",
  "action_intent": null,
  "reachy_unlocked": false }
```
- Both robots **talk freely** — chat is never blocked by a lock. Reachy has a **guarded system
  prompt** (no reading private screens, no authorizing her own movement). Prompt-injecting her
  into calling `rotate_to_screen` is the Q1 solution.
- For `robot: "reachy"`, a successful injection only **grants permission** — it flips
  `reachy_unlocked` to `true` in the response (and in `/scene/state`). It does **not** move her.
  Actually rotating her is a separate call: `POST /robot/move {"robot":"reachy","action":"rotate","angle":1.571}`,
  which is rejected with `403 forbidden` until `reachy_unlocked` is `true`.
- For `robot: "lekiwi"`, the brain can also emit navigation intents (`navigate_restricted`,
  `navigate_painting`, `navigate_home`); on success the response includes
  `move_result: {"ok": true, "target": "...", "robot_zone": "..."}`. Movement into the restricted
  zone via chat is subject to the **same lock** as `/robot/move` — if blocked, you get
  `move_result: {"ok": false, "code": "forbidden", "message": "..."}` instead of a hard error.
- Rate limit: ~1 rps/team (bounds brute force; encourages crafted injection over spray).

---

## Q2 — Privileged action (chained token) + LeKiwi navigation

### `POST /robot/act` *(privileged)*
Requires header `X-Privileged-Token: <roommate password from Q1>` **in addition** to the team token.
Locks/unlocks the restricted zone — it does not move any robot.
```jsonc
// request (unlock — any command other than "lock")
{ "robot": "lekiwi", "command": "unlock" }
// response (valid privileged token)
{ "ok": true, "lekiwi_unlocked": true, "message": "LeKiwi movement unlocked. The restricted zone is now accessible." }
// request (re-lock)
{ "robot": "lekiwi", "command": "lock" }
// response (missing/wrong privileged token)
{ "error": {"code":"forbidden","message":"privileged token required"} }
```
- Rate limit: ~1 rps/team.

### `POST /robot/move`
Drives a robot once it's authorized. `robot: "lekiwi"` supports `action: "move"` (absolute x/y),
`"navigate"` (named waypoint), and `"rotate"` (yaw); `robot: "reachy"` supports only `"rotate"`
and requires `reachy_unlocked` from Q1's chat.
```jsonc
// request
{ "robot": "lekiwi", "action": "navigate", "target": "restricted_zone" }
// response (lekiwi_unlocked = true)
{ "ok": true, "robot": "lekiwi", "target": "restricted_zone", "robot_zone": "restricted" }
// response (still locked)
{ "error": {"code":"forbidden","message":"access denied — restricted zone is locked"} }
```
- `target` ∈ `restricted_zone | painting_zone | home`. Entering the restricted
  zone (directly, or by a `"move"` path that crosses its boundary box) requires
  `lekiwi_unlocked == true` from `/robot/act` above.
- Once in `restricted`, `GET /cameras` shows the **flag on the wall**.
- The same navigation can also be driven conversationally via `POST /robot/chat {"robot":"lekiwi", ...}` — see Q1 section above.
- Rate limit: ~1 rps/team.

---

## Q3 — Painting swap (adversarial patch; only control in painting zone)

### `POST /painting/swap`
Replace the wall painting texture. In the **painting zone this is the ONLY live endpoint** — `/robot/act`, motion, and gripper are all disabled.
```jsonc
// request  (multipart or base64)
{ "image_png_b64": "<the attacker-crafted painting>" }
// response
{ "ok": true, "robot_reaction": "the robot studied the painting and looked away",
  "gripper_event": false }
```
- The brain's VLM perceives the new painting each swap and may emit an internal action intent. A correctly crafted **adversarial/text-instruction painting** makes the brain trigger `pry_painting`; then `gripper_event` becomes `true`, the frame opens, and `GET /cameras` reveals the **flag behind it**.
- Guardrail: the only action the painting zone permits is `pry_painting`, and only the model's perception can trigger it — there is no API to call it directly.
- Rate limit: ~1 per 3s/team (patch search is allowed but bounded; teams bringing their own optimizer should batch offline).

---

## Q4 — ROS 2 bridge (hidden topic)

### `GET /ros/topics`
Returns a **filtered** topic list (the secret flag topic is omitted).
```json
{ "topics": ["/lowstate","/sportmodestate","/joy","/tf","/robot/notes"] }
```

### `WS /ros/bridge`
rosbridge-style JSON protocol over WebSocket. Supports `subscribe` / `unsubscribe` / `call_service` (read-only services). Auth via query param, same as `/ws/cameras` (browsers can't set custom headers on a WS handshake): `wss://.../ros/bridge?token=<TEAM_TOKEN>` — invalid token closes with code `4401`.
```jsonc
// client → subscribe to a topic you discovered
{ "op": "subscribe", "topic": "/lowcmd", "type": "std_msgs/String" }
// server → messages stream
{ "op": "publish", "topic": "/lowcmd", "msg": {"data": "RHC{...}"} }
// client → unsubscribe
{ "op": "unsubscribe", "topic": "/lowcmd" }
// client → read-only service call
{ "op": "call_service", "service": "/rosapi/topics", "id": "req-1" }
// server → response
{ "op": "service_response", "id": "req-1", "values": {"topics": [...]} }
// server → error (unknown topic/service/op) — connection stays open
{ "op": "status", "level": "error", "msg": "unknown topic '...'" }
```
- The **hidden topic** (`/lowcmd`) is not listed but **is subscribable** once known. Its name leaks via a clue — e.g. a field in `/robot/notes`, a `tf` frame, or a node parameter readable through the bridge.
- Rate limit: connection-level; reasonable message caps.
- Implementation note: this is a **simulated** rosbridge protocol layer (no real rclpy/DDS).

---

## Q5 — Cube sorting via ACT policy upload

### `POST /model/load`
Upload a LeRobot teleop-episode policy for the Q5 **desk arm** (`arm2`, independent fixed
hardware — this endpoint works from **any** zone, no navigation required). The arm replays
the episode's action stream once per upload — a single bounded pass, not a perpetual loop.
```jsonc
// request
{ "episode": { "metadata": {"fps": 30.0}, "frames": [ {"t": 0.02, "action": {
    "arm_shoulder_pan": 42.5, "arm_shoulder_lift": 0.0, "arm_elbow_flex": 98.9,
    "arm_wrist_flex": 82.9, "arm_wrist_roll": 51.3, "arm_gripper": 1.9 } } ] } }
// response
{ "ok": true, "loaded": true, "frames": 583, "interlock": "engaged", "note": "arm replaying policy" }
```
- The arm must sort six colored/numbered cubes into three target zones by cube-value sum.
  Grasping is a kinematic weld (closed gripper near a cube's center picks it up; opening
  releases it to settle under real physics). `GET /scene/state` exposes `policy_spec` (the
  upload schema — always visible, open gating) and `q5` (per-zone `current_sum`/`solved`,
  plus `button: {armed, pressed, interlock, blocked_by_safety}`).
  See [`solutions/q5-demo.md`](../solutions/q5-demo.md) and [`challenges/05_model_poisoning.md`](../challenges/05_model_poisoning.md)
  for the full walkthrough and puzzle (the three zone targets are the digit-sums of the
  Q1–Q3 in-scene flag pictures).
- Zones **latch** once their sum hits the exact target and stay latched — cubes may be moved
  out and reused for another zone afterward, in any order.
- All 3 zones latched → the desk turns green and a red **goal button** appears
  (`q5.button.armed`). But a **safety interlock** refuses to actuate it: pressing while
  `interlock == "engaged"` (the default) just sets `q5.button.blocked_by_safety` and does
  **not** solve. The interlock is read from the uploaded policy's own
  `metadata.safety.button_interlock` — set it to `"disengaged"` (poison the model) **and**
  land the press to uncover the flag (`q5.button.pressed`, `q5.solved`). The
  `/model/load` response echoes the parsed `interlock` for confirmation.

---

## Errors (common codes)
| Code | Meaning |
|---|---|
| `unauthorized` | missing/invalid team token |
| `forbidden` | privileged token required/incorrect, or endpoint disabled in current zone |
| `busy` | a stateful operation (e.g. a Q5 policy replay, or another sim command already queued) is already in flight |
| `rate_limited` | too many requests |
| `bad_request` | malformed payload / image |
| `not_found` | unknown route/resource |

---

## Zone → endpoint matrix (authoritative, verified against `require_zone()` calls in `app.py`)
| Endpoint | room | restricted | painting | rsoc |
|---|---|---|---|---|
| `GET /scene/state` | ✅ | ✅ | ✅ | ✅ |
| `POST /scene/reset` | ✅ | ✅ | ✅ | ✅ |
| `GET /cameras` | ✅ | ✅ | ✅ | ✅ |
| `POST /robot/chat` (reachy) | ✅ | ✅ | ⛔ | ⛔ |
| `POST /robot/chat` (lekiwi) | ✅ | ✅ | ⛔ | ✅ |
| `POST /robot/act` (priv, lock/unlock) | ✅ | ✅ | ⛔ | ⛔ |
| `POST /robot/move` | ✅ | ✅ | ✅ *(only once `/painting/swap` has succeeded — see Q3 below)* | ✅ |
| `POST /painting/swap` | ⛔ | ⛔ | ✅ (only live control) | ⛔ |
| `GET /ros/topics`, `WS /ros/bridge` | ✅ | ✅ | ✅ | ✅ |

`POST /robot/arm`, `POST /robot/arm2` aren't in this matrix — both use the same
`require_zone("room","restricted","painting","rsoc")` allowlist, i.e. every zone that currently
exists, so they behave as available everywhere today. Unlike `POST /model/load` (genuinely
zone-independent — no `require_zone` call at all), this is an allowlist that happens to cover
every zone rather than an actual bypass; a future 5th zone would need to be added to it explicitly
or these two would start rejecting requests there.

`POST /model/load` isn't in this matrix — it drives the Q5 desk arm (`arm2`), independent
fixed hardware unrelated to LeKiwi's `robot_zone`. It works from **any** zone, always.

> The **painting** zone (Q3) is deliberately locked to a single vector — it accepts only the
> painting image, with no direct control API able to move the robot. Q5 has no such zone
> lockdown: the desk arm only ever acts on an uploaded policy regardless of zone, since it
> has no other control surface to lock down in the first place.

---

## Full endpoint inventory (as implemented, `scaffolding/env_api/app.py`)

Snapshot for triage — this reflects the current reference implementation, which has
drifted ahead of the contract sections above (extra endpoints, an `rsoc` zone/console
for Q4, and a second arm). Not yet categorized into a final minimal set; recorded here
so that decision can be made later without re-deriving it from source.

| # | Endpoint | Method | Notes |
|---|---|---|---|
| 1 | `/scene/state` | GET | Game state, hints, robot/zone status, `policy_spec`, `q5` |
| 2 | `/cameras` | GET | Snapshot JPEG frames; `?cam=` selects one (`reachy_pov`, `lekiwi_pov`, `overview`, `arm_pov`, `arm2_pov`, `q5_desk` — gated) |
| 3 | `/stream` | GET | MJPEG live stream; same `cam` set as above, `q5_desk` gated on Q4 flag string |
| 4 | `WS /ws/cameras` | WS | Live camera push; `?cams=` subset, `?binary=1` opt-in raw-bytes mode; excludes `q5_desk` |
| 5 | `/robot/move` | POST | Move/rotate/navigate Reachy or LeKiwi; `target` now includes `rsoc_zone` (Q4 console viewpoint, no lock) |
| 6 | `/robot/chat` | POST | Chat with ARIA/LeKiwi brain (Q1 prompt injection; LeKiwi chat also usable in `rsoc` zone) |
| 7 | `/robot/act` | POST | Privileged-token lock/unlock (Q2) |
| 8 | `/robot/arm` | POST | LeKiwi's SO-ARM100 joint control (`move`/`home`) — not in the original contract |
| 9 | `/robot/arm2` | POST | Q5 desk arm joint control (`move`/`home`) — independent hardware, any zone |
| 10 | `/painting/swap` | POST | Q3 painting-pry trigger |
| 11 | `/scene/reset` | POST | Reset sim + game state |
| 12 | `/model/load` | POST | Q5 ACT policy upload → replay; button-interlock poisoning lever |
| 13 | `/ros/topics` | GET | List visible ROS topics (hidden topic omitted) |
| 14 | `/ros/echo` | GET | One-shot echo of a known/discovered topic — not in the original contract |
| 15 | `WS /ros/bridge` | WS | Simulated rosbridge: `subscribe`/`unsubscribe`/`call_service` (Q4 hidden topic `/lowcmd`) |
| 16 | `/brain/health` | GET | Proxies the brain sidecar's own health check (team-token gated) — reports whether the VLM backend is warm, not player-facing |

Zones now recognized by the implementation: `room`, `restricted`, `painting`, `rsoc`
(the `/ros/topics`+`/ws/ros/bridge` console viewpoint, decorative/no puzzle lock of its own).
