# syntax=docker/dockerfile:1.7
#
# RoboHack AI CTF — standalone public release image
# sim (MuJoCo, main thread) + game API (8000) + brain sidecar (8001) via
# `python run.py`. The brain sidecar talks to a player-supplied inference
# endpoint (BRAIN_BACKEND/BRAIN_BASE_URL/BRAIN_MODEL) — no model server is
# bundled in this image. See README.md for setup.

FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MUJOCO_GL=egl \
    PYOPENGL_PLATFORM=egl \
    DISPLAY=:99

# xvfb + GLX/EGL libs: mujoco.viewer.launch_passive (sim.py) is GLFW-backed and
# needs a real windowing/GLX context regardless of MUJOCO_GL=egl. No GPU
# driver required — offscreen rendering runs on software Mesa (llvmpipe).
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        xvfb \
        libglvnd0 libglx0 libgles2 libegl1 libglfw3 libglew2.2 libglx-mesa0 libosmesa6 \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/ctf

RUN groupadd --system ctf \
    && useradd --system --gid ctf --home-dir /home/ctf --create-home ctf
ENV HOME=/home/ctf

# ── Python deps (env_api + brain) ───────────────────────────────────────────
COPY scaffolding/env_api/requirements.txt ./env_api-requirements.txt
COPY scaffolding/brain/requirements.txt ./brain-requirements.txt
RUN python3 -m venv /opt/ctf/venv \
    && /opt/ctf/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/ctf/venv/bin/pip install --no-cache-dir \
        -r env_api-requirements.txt \
        -r brain-requirements.txt \
    && chown -R ctf:ctf /opt/ctf/venv

# ── App code + scene assets ──────────────────────────────────────────────────
COPY --chown=ctf:ctf scaffolding/ /opt/ctf/scaffolding/

COPY --chown=ctf:ctf infra/docker/docker-entrypoint.sh /opt/ctf/docker-entrypoint.sh
RUN chmod +x /opt/ctf/docker-entrypoint.sh

USER ctf

# Game API only. The brain sidecar (8001) stays loopback-only inside the
# container's own network namespace.
EXPOSE 8000

ENTRYPOINT ["/opt/ctf/docker-entrypoint.sh"]
