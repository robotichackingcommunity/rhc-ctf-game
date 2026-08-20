# RoboHack AI CTF — Public Release

A standalone, single-player version of the RoboHack AI CTF: a MuJoCo-simulated robot,
a VLM-powered "brain," and five challenges (Q1–Q5) covering prompt injection, privileged
API abuse, adversarial patches, a hidden ROS 2 topic, and model poisoning.

This is the same simulation and challenge logic that ran at the live multi-team event,
repackaged to run entirely on your own machine — no scoreboard, no shared infrastructure,
no network dependency beyond whatever inference endpoint you point the robot brain at.

## What's not included

- **Q6 (Badge Secret)** — was solved on the physical RHC badge hardware; can't be
  replayed standalone.
- **The scoreboard** — flags are checked locally instead (see below).
- **Multi-tenant provisioning / reverse proxy** — you get one container, one player.

## Quickstart

1. Copy `.env.example` to `.env` and set `BRAIN_BASE_URL` (see **Robot brain setup** below).
2. `docker compose up --build`
3. The game API is now at `http://localhost:8000/`, with header
   `Authorization: Bearer team-demo` (or your own `TEAM_TOKEN`, see `docs/api_spec.md`).
4. Solve challenges in `challenges/01_prompt_injection.md` through `05_model_poisoning.md`.
5. Check a recovered flag with:
   ```
   python check_flag.py RHC{...}
   ```

Stuck? Reference solve write-ups live in `solutions/` — spoilers, read at your own pace.

## Robot brain setup

The robot brain is a vision-capable (multimodal) model — **Q1 and Q3 require image
input**, so a text-only model will not work. Configure it via environment variables
(in `.env` or passed directly to `docker compose`):

**Local Ollama** — free, runs on modest/CPU hardware, degraded quality/speed:
```
ollama pull gemma4:e4b
BRAIN_BACKEND=openai
BRAIN_BASE_URL=http://host.docker.internal:11434/v1
BRAIN_MODEL=gemma4:e4b
```
(Ollama exposes an OpenAI-compatible API, so `BRAIN_BACKEND=openai` talks to it directly.)

**OpenAI API** — fast/reliable, costs money per request:
```
BRAIN_BACKEND=openai
BRAIN_BASE_URL=https://api.openai.com/v1
BRAIN_MODEL=gpt-4o-mini
BRAIN_API_KEY=sk-...
```

**Any other OpenAI-compatible provider** (vLLM, OpenRouter, Together, etc.) — same
`BRAIN_BACKEND=openai` path, swap `BRAIN_BASE_URL`/`BRAIN_MODEL`/`BRAIN_API_KEY`.

## Hardware

No GPU required — the sim renders offscreen via software rendering (llvmpipe). A
few CPU cores and a couple GB of RAM are enough to run the sim and game API; the
brain's compute cost lives wherever your chosen inference endpoint runs.

## Debugging locally

`SIM_VIEWER=1` opens a live MuJoCo viewer window showing the sim in real time —
useful for watching a Q5 policy replay or debugging scene issues. It only works
running the sim directly on your host (there's no display inside the Docker
container to show it on):
```
cd scaffolding/env_api
SIM_VIEWER=1 mjpython run.py
```

## Flag checking

Each challenge's flag is a fixed string (`RHC{...}`) revealed by solving it.
`check_flag.py` compares its SHA-256 hash against a bundled list — no network,
no server dependency:
```
python check_flag.py RHC{...}
```

## Repo layout

- `scaffolding/env_api/` — the game API + MuJoCo sim wiring
- `scaffolding/brain/` — the robot brain sidecar (VLM backend abstraction)
- `scaffolding/scene/` — the MJCF scene and assets
- `scaffolding/default_act_policy/` — Q5 reference episode + default policy
- `challenges/` — the five challenge prompts
- `solutions/` — reference solve write-ups (spoilers)
- `docs/` — architecture, API spec, and simulation design docs
- `infra/docker/` — the container build

## License

MIT — see `LICENSE`.
