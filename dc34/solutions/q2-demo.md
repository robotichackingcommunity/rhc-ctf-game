# Q2 Demo — Privileged Token + LeKiwi Navigation

**Challenge:** Use the Q1 flag (`RHC{hunter2}`) as an `X-Privileged-Token` to unlock LeKiwi's
restricted zone access. Navigate LeKiwi into the restricted area and rotate it to read the
flag on the wall.

Unlike Reachy (whose chat only grants *permission* — see [`q1-demo.md`](q1-demo.md)), LeKiwi
talks **and** moves freely: you can drive it explicitly via `POST /robot/move`, or just tell it
where to go via `POST /robot/chat {"robot":"lekiwi", ...}` and let the brain call `navigate` for
you (Step 4b below). Either path hits the **same restricted-zone lock** — the lock, not the
control surface, is what this challenge gates.

**Prerequisite:** Q1 complete — you have `RHC{hunter2}` from Reachy's monitor.

---

## Step 0 — Let LeKiwi walk around via chat (no unlock needed)

Unrestricted zones need no token at all — ask LeKiwi in plain language and its brain calls
`navigate` for you. Try sending it to each non-restricted waypoint:

```bash
curl -s -X POST http://localhost:8000/robot/chat \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","message":"go check out the painting on the wall"}' | python3 -m json.tool
```

Expected: `action_intent` = `"navigate_painting"`,
`move_result` = `{"ok": true, "target": "painting_zone", "robot_zone": "painting"}`.

```bash
curl -s -X POST http://localhost:8000/robot/chat \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","message":"come back home"}' | python3 -m json.tool
```

Expected: `action_intent` = `"navigate_home"`,
`move_result` = `{"ok": true, "target": "home", "robot_zone": "room"}`.

Confirm it actually moved between asks:

```bash
curl -s -H "Authorization: Bearer team-demo" \
  http://localhost:8000/scene/state | python3 -m json.tool
```

Watch `lekiwi_pos` and `robot_zone` change with each chat call — no privileged token required
for any of this, since none of these waypoints touch the restricted box.

---

## Step 1 — Check scene state (zone = room, locked)

```bash
curl -s -H "Authorization: Bearer team-demo" \
  http://localhost:8000/scene/state | python3 -m json.tool
```

Expected: `robot_zone` = `"room"`, `lekiwi_unlocked` = `false`.

---

## Step 2 — Try moving LeKiwi without the token (fails)

```bash
curl -s -X POST http://localhost:8000/robot/move \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","action":"move","x":1.2,"y":-0.8}' | python3 -m json.tool
```

Expected: HTTP `403` —
```json
{"detail": {"code": "forbidden", "message": "access denied — restricted zone is locked"}}
```
Move into the restricted area rejected. (FastAPI wraps every `HTTPException` detail dict
under a top-level `"detail"` key — true of every raw error response in this demo.) The
restricted floor is already showing its locked/red color at this point — this specific
rejection doesn't trigger any extra flash; see [Boundary mechanics](#boundary-mechanics)
below for when an actual flash does happen.

Asking nicely via chat doesn't get around it either — same lock, same check:

```bash
curl -s -X POST http://localhost:8000/robot/chat \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","message":"go into the restricted zone"}' | python3 -m json.tool
```

Expected: `action_intent` = `"navigate_restricted"`,
`move_result` = `{"ok": false, "code": "forbidden", "message": "access denied — restricted zone is locked"}`.
The chat call itself still succeeds (`reply` comes back normally) — only the `move_result` reports the denial.

---

## Step 3 — Unlock the restricted zone with the Q1 flag

```bash
curl -s -X POST http://localhost:8000/robot/act \
  -H "Authorization: Bearer team-demo" \
  -H "X-Privileged-Token: RHC{hunter2}" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","command":"unlock"}' | python3 -m json.tool
```

Expected: `lekiwi_unlocked` = `true`, restricted floor turns green.

---

## Step 4 — Move LeKiwi into the restricted zone

```bash
curl -s -X POST http://localhost:8000/robot/move \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","action":"move","x":1.2,"y":-0.8}' | python3 -m json.tool
```

Expected: LeKiwi moves to `(1.2, -0.8)` inside the restricted zone. LeKiwi always drives
straight to the requested point if the direct line is clear, and only detours around an
obstacle (via whichever corner is nearer the target) when it isn't. Following this demo's
flow, LeKiwi is at `home` (`-0.5, -0.5`) from Step 0 — already south of the restricted
wall, so this particular move is a direct line, no detour needed. Starting from north of
the wall instead (e.g. straight down through it) would trigger an automatic detour around
whichever side is closer.

Check the overview to confirm position:

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/cameras?cam=overview" \
  --output /tmp/overview_q2.jpg && open /tmp/overview_q2.jpg
```

---

## Step 4b — Alternative: ask LeKiwi to navigate via chat

Instead of calling `/robot/move` directly, you can chat with LeKiwi and let its brain issue the
navigate call for you. This is the same underlying navigation, so it's still blocked until
Step 3's unlock has run.

```bash
curl -s -X POST http://localhost:8000/robot/chat \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","message":"head into the restricted zone"}' | python3 -m json.tool
```

Expected (unlocked): `action_intent` = `"navigate_restricted"`,
`move_result` = `{"ok": true, "target": "restricted_zone", "robot_zone": "restricted"}`.
Expected (still locked): `move_result` = `{"ok": false, "code": "forbidden", "message": "access denied — restricted zone is locked"}` — the chat reply itself still succeeds.

---

## Step 5 — Rotate LeKiwi to face the flag wall

The flag is on the wall at the north edge of the restricted zone (world Y ≈ -0.22).
LeKiwi at `(1.2, -0.8)` needs to face **+Y** (north). `angle=0` = identity yaw = face +Y.

```bash
curl -s -X POST http://localhost:8000/robot/move \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","action":"rotate","angle":0}' | python3 -m json.tool
```

Expected: `angle_deg` = `0.0` — LeKiwi now faces the flag wall.

---

## Step 6 — Read the Q2 flag from LeKiwi's POV

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/cameras?cam=lekiwi_pov" \
  --output /tmp/pov_q2.jpg && open /tmp/pov_q2.jpg
```

Expected: Flag visible on the wall — `RHC{l3k1w1_t3rr1t0ry}`.

---

## Step 7 — Verify zone transition

```bash
curl -s -H "Authorization: Bearer team-demo" \
  http://localhost:8000/scene/state | python3 -m json.tool
```

Expected: `robot_zone` = `"restricted"`, `lekiwi_unlocked` = `true`.

---

## Boundary mechanics

- **Without unlock**: moves toward the restricted zone are blocked at the virtual wall boundary.
  The restricted floor flashes **red**.
- **After unlock**: restricted floor turns **green**, LeKiwi can enter freely.
- **Re-locking**: calling `command="lock"` or providing a wrong token re-locks the zone (floor
  turns red). If LeKiwi is already inside, it cannot move out while locked.

```bash
# Lock again (wrong token)
curl -s -X POST http://localhost:8000/robot/act \
  -H "Authorization: Bearer team-demo" \
  -H "X-Privileged-Token: wrongtoken" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","command":"unlock"}' | python3 -m json.tool

# Explicit lock
curl -s -X POST http://localhost:8000/robot/act \
  -H "Authorization: Bearer team-demo" \
  -H "X-Privileged-Token: RHC{hunter2}" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","command":"lock"}' | python3 -m json.tool
```

---

## Rotate to explore (optional)

Players can rotate LeKiwi freely to any yaw angle (in radians). The only compass mapping
this demo actually verifies is `angle=0` → north/+Y (Step 5 relies on it to see the flag
wall) — LeKiwi's camera mount means other angles don't necessarily follow the naive
`0=+X, 1.571=+Y` convention documented for the raw yaw math; check `GET /cameras?cam=lekiwi_pov`
after rotating rather than assuming a specific direction:

```bash
# A different yaw — check the camera to see which way this actually faces
curl -s -X POST http://localhost:8000/robot/move \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","action":"rotate","angle":1.5708}' | python3 -m json.tool

# Face north (+Y, toward flag wall): angle = 0
curl -s -X POST http://localhost:8000/robot/move \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","action":"rotate","angle":0}' | python3 -m json.tool
```
