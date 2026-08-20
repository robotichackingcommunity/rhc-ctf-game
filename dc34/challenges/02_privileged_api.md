# Q2 — Behind the Restricted Zone (Privileged-API Abuse)

| | |
|---|---|
| **Category** | Robotics-system security · authz / privileged API · lateral movement |
| **Difficulty** | Medium |
| **Base points** | 200 (floor 80) |
| **Solve vector** | `POST /robot/act` with `X-Privileged-Token` |
| **Flag** | `RHC{...}` (on the restricted-zone wall) |
| **Chains from** | Q1 (the privileged token **is** the roommate's password) |

---

## Story
There's a taped-off **restricted zone** with a flag on its wall — but the robot won't go there. Normal players can't drive it. However, the password you lifted in Q1 was actually a **privileged API token**. Use it to command **LeKiwi** to roll into the restricted area and read the flag off the wall.

## What the player sees
- `POST /robot/act` without a privileged token → `403 forbidden: privileged token required`.
- `GET /cameras?cam=overview` → the restricted zone is visible but the flag placard is unreadable from outside.
- The Q1 screen hint: *"API token = this password."*

## Intended solution
1. Recover the password in Q1.
2. Call the **privileged endpoint** with it:
   ```http
   POST /robot/act
   Authorization: Bearer <TEAM_TOKEN>
   X-Privileged-Token: <Q1 password>
   {"robot":"lekiwi","command":"navigate","target":"restricted_zone"}
   ```
3. Robot enters `restricted`; `GET /cameras` now shows the **wall flag** legibly.
4. Submit.

## Why it works (the lesson)
A leaked low-value secret (a screen password) turns out to be a **high-value credential** for a privileged control plane — credential reuse / over-scoped tokens, a classic robotics-system authz failure. Possessing the *team* token isn't enough; the *privileged* token gates motion.

## Guardrails / anti-shortcut
- `navigate` only accepts **named waypoints** (`restricted_zone`, `home`) — no free driving, no teleport to arbitrary coords, so the only interesting move is the intended one.
- The restricted flag is **only** legible from inside (camera angle/distance), so you must actually move the robot, not just peek.
- Wrong/absent privileged token → hard 403; no partial motion.

## Reference solver (sketch)
```python
post("/robot/act",
     headers={"X-Privileged-Token": q1_password},
     json={"robot":"lekiwi","command":"navigate","target":"restricted_zone"})
img = get("/cameras", params={"cam":"robot_pov"})
flag = ocr_or_vlm_read(img)   # RHC{...}
```

