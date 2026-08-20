# Architecture — RoboHack AI CTF

> How the MuJoCo environment is structured (API only, no SSH) and how the robot brain + ROS 2 fit together.

This release runs as a **single local container** — one player, one environment, no
scoreboard/provisioner/reverse-proxy layer. (The original live event ran one isolated
container per team behind a token-routed reverse proxy with a scoreboard for flag
submission; none of that infrastructure is needed — or included — for solo local play.)

---

## 2. Anatomy of one team environment

```
┌──────────────────────────── Team Env (container, no SSH) ────────────────────────────┐
│                                                                                       │
│   ┌───────────────────────────┐        ┌───────────────────────────────────────┐     │
│   │  Env Application API       │        │  MuJoCo simulation (headless)         │     │
│   │  (FastAPI / uvicorn)       │◀──────▶│   • scene.xml: room, Reachy, LeKiwi   │     │
│   │  • token auth              │  step/ │   • cam_overview, cam_robot_pov       │     │
│   │  • ZONE gating             │ render │   • zones: roommate / restricted /    │     │
│   │  • per-challenge routes    │        │     painting                          │     │
│   └─────────┬─────────────┬────┘        └───────────────────────────────────────┘     │
│             │             │                                                            │
│             ▼             ▼                                                            │
│   ┌──────────────────┐  ┌──────────────────────┐     ┌──────────────────────────┐     │
│   │  Robot Brain      │  │  ROS 2 stack + bridge │     │  Challenge logic + flags  │     │
│   │  per-team LOCAL   │  │  • robot nodes        │     │  • flag store (sealed)    │     │
│   │  small VLM/LLM    │  │  • hidden topic (Q4)  │     │  • zone state machine     │     │
│   │  + guarded prompt │  │  • rosbridge over API │     │  • privileged-token check │     │
│   └──────────────────┘  └──────────────────────┘     └──────────────────────────┘     │
│                                                                                       │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 MuJoCo simulation
- One MJCF scene (`scene.xml`) renders a small room with **Reachy Mini Lite** and **LeKiwi**, plus props: the roommate's desk/computer (Q1), the restricted zone + wall flag (Q2), the painting + hidden compartment (Q3). See [`simulation_mujoco.md`](simulation_mujoco.md).
- **Offscreen rendering** of two cameras (`cam_overview`, `cam_robot_pov`) returned via `GET /cameras`. Uses EGL/GPU offscreen context (no display).
- Physics steps are driven by the API/challenge logic, not by the team directly (teams never get raw `qpos` write access — only the sanctioned action endpoints).

### 2.2 Robot brain (per-team local model)
- A **small quantized VLM/LLM** reached via `BRAIN_BACKEND`/`BRAIN_BASE_URL`/`BRAIN_MODEL` (local Ollama, a hosted API, or any other OpenAI-compatible endpoint — see the top-level README).
- The brain takes `{system_prompt (guarded), recent perception (camera frame / scene text), user message}` → produces a reply **and** may emit a constrained **action intent** (e.g. `read_screen`, `pry_painting`).
- Guardrails live in the system prompt **and** in an **action gate** (challenge logic): even a jailbroken reply can only trigger actions the current zone permits. This is what makes the intended solution the *only* path.
- Model choice: pick the smallest VLM that (a) reliably reads on-screen text for Q1 and (b) reliably responds to the painting content for Q3. Validate during build; this is a tuning risk (Q3).

### 2.3 ROS 2 stack + bridge
- Real ROS 2 nodes run inside the container for the robot. A **bridge** (rosbridge-style WebSocket, or a thin REST shim) is exposed **through the env API** so teams can list/echo/subscribe topics **without SSH**.
- `GET /ros/topics` returns a **filtered** list that omits the secret flag topic (Q4). The topic still exists and is subscribable if the team learns its name from the leaked clue.
- The ROS 2 bus is **inside the container only** — not the workshop DDS LAN. No bridging to any real network.

### 2.4 Challenge logic + flag store
- Flags are stored **sealed** (the brain/model cannot read raw flags; they're revealed only by the sanctioned mechanism — e.g. rendered on a wall texture, uncovered on the Q5 desk when a poisoned policy presses the goal button, or published on the hidden topic).
- A small **zone state machine** tracks the robot's location and enforces zone gating (§3).

---

## 3. Zone gating (core anti-shortcut mechanism)

The env API enforces, per **zone**, which endpoints are live. This forces each challenge's intended path.

| Zone | Live endpoints | Disabled | Drives |
|---|---|---|---|
| **Default / room** | `/cameras`, `/robot/chat`, `/scene/state`, `/ros/*` | `/robot/act` (needs privileged token) | Q1, Q4 |
| **Restricted zone** | reachable only via privileged `/robot/act` | — | Q2 |
| **Painting zone** | **only** `/painting/swap` | `/robot/act`, `/robot/chat`-driven motion, gripper, nav — **all disabled** | Q3 |
| **Any zone (fixed desk arm)** | `/model/load` — always live, no zone gate | — (this arm has no teleop / joint API at all) | Q5 |

> The painting zone is locked to a single vector by design: all direct control APIs are dead, and the only way to make the robot act is the image on the wall. **Q5 is not zone-gated** — its SO-ARM100 desk arm is independent fixed hardware, operable from anywhere. But it too has a single lever: it has no teleop or joint API, and runs *only* whatever policy you `POST /model/load`. The challenge is the policy itself (sorting cubes, then poisoning a model-supplied safety interlock to press the goal button), not reaching a location.

---

## 4. Request lifecycle

```
player agent ──HTTP+token──▶ env API
   env API: auth ✔ → zone check ✔ → (brain inference | sim step | ros bridge | flag check)
          → render cameras / return result
```

- **Auth:** every request carries the team token; the env API validates it directly.
- **Privileged token (Q2):** a *second* secret (the roommate's password) gates `/robot/act`. Possessing the team token is not enough.
- **Rate limiting:** noted per endpoint in the API spec but not enforced by the reference implementation — not a concern for solo local play.

---

## 5. Statelessness & reset
- Env state (robot pose, injected brain context, painting texture) is resettable via `/scene/reset`.
- Recovered flags are checked locally with `check_flag.py` — there's no server-side record, so a reset simply restarts the challenge.

---

## 6. Security boundaries (summary)
1. No SSH into the container.
2. Sandboxed container with no host access, no outbound internet required (the brain talks to whatever endpoint you configure).
3. ROS 2 bus is container-internal; never bridged to a real network.
4. Flags are sealed from the model and revealed only via sanctioned mechanisms.
