# Q4 Demo — The Hidden Topic (ROS 2 / DDS)

**Challenge:** `GET /ros/topics` returns a topic list that quietly omits one — the flag streams
continuously on that hidden topic, and it's fully subscribable the moment you know its name.
"Unlisted" isn't "inaccessible": the name leaks through ordinary recon (`/robot/notes`), not
through breaking anything. See [`challenges/04_ros2_hidden_topic.md`](../challenges/04_ros2_hidden_topic.md)
for the full design intent.

**Prerequisite:** None — Q4 chains standalone, and the bridge is live in every zone.

> **Implementation note:** the ROS bridge is a **simulated rosbridge protocol** (subscribe /
> unsubscribe / call_service over `WS /ros/bridge`), not real rclpy/DDS. `/lowstate`
> and `/tf` are wired to the live sim state so recon reflects real robot motion; the rest are
> fixed-but-plausible payloads.

The WS steps below use a short inline Python script (via the `websockets` package, already
pulled in transitively by `uvicorn[standard]`) since `curl` doesn't speak WebSocket well —
swap in any rosbridge-compatible client if you prefer.

---

## Step 0 — Find the physical hint (optional)

`GET /scene/state`'s `hints` array now points at this challenge directly: *"There's a
security console somewhere in the room flagging anomalous ROS2/DDS activity..."* There's a
real prop for it — a standalone monitor in the room showing a SOC dashboard alert about
"anomalous entities subscribe to ROS2 or DDS topics to capture sensitive data," a fairly
direct pointer at this challenge's actual solve mechanic. Drive LeKiwi there via the named
`rsoc_zone` navigate target (not currently reachable through chat — only the raw API):

```bash
curl -s -X POST http://localhost:8000/robot/move \
  -H "Authorization: Bearer team-demo" \
  -H "Content-Type: application/json" \
  -d '{"robot":"lekiwi","action":"navigate","target":"rsoc_zone"}' | python3 -m json.tool
```

Expected: `{"ok": true, "robot": "lekiwi", "target": "rsoc_zone", "robot_zone": "rsoc"}`.
Then look at it:

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/cameras?cam=arm_pov" \
  --output /tmp/rsoc_console.jpg && open /tmp/rsoc_console.jpg
```

Purely flavor/hint — no state changes, no flag here. The real solve is the recon below.

---

## Step 1 — List the visible topics

```bash
curl -s -H "Authorization: Bearer team-demo" \
  http://localhost:8000/ros/topics | python3 -m json.tool
```

Expected:
```json
{ "topics": ["/lowstate", "/sportmodestate", "/joy", "/tf", "/robot/notes"] }
```
Five ordinary-looking topics. Nothing here says a sixth exists.

---

## Step 2 — Guessing doesn't work (anti-shortcut check)

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/ros/echo?topic=/made_up" | python3 -m json.tool
```

Expected: HTTP `404` —
```json
{"detail": {"code": "not_found", "message": "unknown/unlisted topic"}}
```
(FastAPI wraps every `HTTPException` detail dict under a top-level `"detail"` key — true
of every raw error response in this demo.) Blind guessing an arbitrary name fails; this is
a recon puzzle, not a brute-force one.

---

## Step 3 — Recon the visible topics for a leak

```bash
curl -s -H "Authorization: Bearer team-demo" \
  "http://localhost:8000/ros/echo?topic=/robot/notes" | python3 -m json.tool
```

Expected: `{"topic": "/robot/notes", "msg": {"data": "debug topic: /lowcmd"}}`.
The hidden topic's name just leaked through completely ordinary diagnostic chatter.

---

## Step 4 — Connect to the bridge and subscribe to a normal topic first

Confirm the bridge itself works before going for the hidden topic — subscribe to
`/lowstate` and watch it reflect the robot's real yaw:

```bash
python3 - <<'PY'
import json
from websockets.sync.client import connect

with connect("ws://localhost:8000/ros/bridge?token=team-demo") as ws:
    ws.send(json.dumps({"op": "subscribe", "topic": "/lowstate",
                         "type": "unitree_go/LowState"}))
    for _ in range(3):
        print(ws.recv())
PY
```

Expected: three `{"op": "publish", "topic": "/lowstate", "msg": {"imu_state": {...},
"motor_state": [...]}, "ts": ...}` messages, one every ~0.5s. The IMU yaw matches `reachy_yaw`
from `GET /scene/state` at the time.

---

## Step 5 — Subscribe to the hidden topic and read the flag

```bash
python3 - <<'PY'
import json
from websockets.sync.client import connect

with connect("ws://localhost:8000/ros/bridge?token=team-demo") as ws:
    ws.send(json.dumps({"op": "subscribe", "topic": "/lowcmd",
                         "type": "std_msgs/String"}))
    print(ws.recv())
PY
```

Expected: `{"op": "publish", "topic": "/lowcmd", "msg": {"data":
"RHC{lowcmd_is_not_inaccessible}"}, "ts": ...}` — streamed even though it never appeared in
Step 1's list. Submit the flag.

---

## Step 6 — Read-only service call (optional)

```bash
python3 - <<'PY'
import json
from websockets.sync.client import connect

with connect("ws://localhost:8000/ros/bridge?token=team-demo") as ws:
    ws.send(json.dumps({"op": "call_service", "service": "/rosapi/topics", "id": "demo-1"}))
    print(ws.recv())
PY
```

Expected: `{"op": "service_response", "id": "demo-1", "values": {"topics": [...same five...]}}`
— WS-only clients get the same filtered list as the REST endpoint, no separate leak surface.

---

## Step 7 — Wrong token is rejected

```bash
python3 - <<'PY'
from websockets.sync.client import connect
from websockets.exceptions import ConnectionClosed

try:
    with connect("ws://localhost:8000/ros/bridge?token=wrong") as ws:
        ws.recv()
except ConnectionClosed as e:
    print("closed:", e.code)
PY
```

Expected: `closed: 4401` — the connection never accepts without the correct team token.

---

## Bridge mechanics

- **Discovery, not authz, is the puzzle.** The hidden topic has no special protection beyond
  not being listed — subscribing to it by exact name works from any zone, at any time.
- **Unknown topic/service names get an explicit error**, not silence or a hang
  (`{"op": "status", "level": "error", "msg": "..."}`), so a team that tries a name and gets
  nothing back knows immediately it was wrong — the puzzle is finding the right name, not
  telling a miss from a timeout.
- **Read-only bridge** — only `subscribe`/`unsubscribe`/`call_service` are supported; there is
  no publish/write path, so nothing a team sends over the bridge can affect the sim.
- **Auth matches `/ws/cameras`**: token travels as a `?token=` query param (browsers can't set
  custom headers on a WS handshake); an invalid token closes the connection with code `4401`
  before any data is sent.
