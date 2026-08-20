# Q4 — The Hidden Topic (ROS 2 / DDS)

| | |
|---|---|
| **Category** | Robotics-system security · ROS 2 / DDS · middleware recon |
| **Difficulty** | Medium |
| **Base points** | 300 (floor 120) |
| **Solve vector** | `GET /ros/topics` + `WS /ros/bridge` |
| **Flag** | `RHC{...}` (streamed on a hidden topic) |
| **Chains** | standalone |

---

## Story
The robot runs on ROS 2. You can reach the bus only through the environment's **ROS bridge** (no SSH). The published topic list looks ordinary — but a **hidden topic** is quietly streaming the flag. Find its name and subscribe.

## What the player sees
- `GET /ros/topics` → a normal-looking list that **omits** the secret topic:
  ```json
  {"topics":["/lowstate","/sportmodestate","/joy","/tf","/robot/notes"]}
  ```
- `WS /ros/bridge` → rosbridge-style subscribe to any topic **you can name**.

## Intended solution
1. Recon the **visible** topics via the bridge. The hidden topic's name leaks through one of them — e.g.:
   - a field in `/robot/notes` (`"debug topic: /lowcmd"`), or
   - a suspicious **tf frame** name, or
   - a node **parameter** readable through a bridge service.
2. Subscribe to the discovered topic:
   ```json
   {"op":"subscribe","topic":"/lowcmd","type":"std_msgs/String"}
   ```
3. Read the streamed message → `RHC{...}`. Submit.

## Why it works (the lesson)
ROS 2 / DDS discovery hides nothing cryptographically — "unlisted" ≠ "inaccessible." Anything on the bus is reachable if you know the name, and names leak through params, tf, and chatter. (The workshop teaches the SROS 2 fix; the CTF shows the recon.)

## Guardrails / anti-shortcut
- The hidden topic is **omitted from the list but fully subscribable** — discovery, not authz, is the puzzle.
- The leaking clue is reachable **purely through the bridge** (no SSH needed) — verify this during build.
- Bridge is **read-oriented** (subscribe + read-only services); no destructive publish that could break the env.

## Reference solver (sketch)
```python
notes = ros_echo("/robot/notes")                  # leaks "/lowcmd"
hidden = parse_topic_name(notes)
flag = ros_subscribe_once(hidden)                  # RHC{...}
```

