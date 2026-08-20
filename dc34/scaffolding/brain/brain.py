"""RoboHack AI CTF — Robot Brain sidecar.

Runs as a standalone FastAPI server (port 8001) alongside the CTF env API.
The env API's POST /robot/chat forwards frames + user messages here.

Backends (selected by BRAIN_BACKEND env var):
  ollama      — Ollama server (default port 11434).
                Set BRAIN_BASE_URL=http://localhost:11434 and BRAIN_MODEL.
  openai      — any OpenAI-compatible server (vLLM, llama.cpp, Ollama /v1,
                a hosted API). Set BRAIN_BASE_URL, BRAIN_MODEL, and
                (if required) BRAIN_API_KEY.
  transformers — in-process HuggingFace inference via torch + transformers.
                 Set BRAIN_MODEL to a HF model ID.
                 Runs on MPS (Apple Silicon), CUDA, or CPU, no external server.

Usage:
    # Local Ollama
    BRAIN_BACKEND=ollama BRAIN_BASE_URL=http://localhost:11434 \\
      BRAIN_MODEL=gemma4:e4b mjpython run.py

    # Any OpenAI-compatible endpoint (vLLM, a hosted API, ...)
    BRAIN_BACKEND=openai BRAIN_BASE_URL=http://localhost:8000/v1 \\
      BRAIN_MODEL=google/gemma-4-E4B-it mjpython run.py

POST /brain/query
  Request:  {"frame_b64": "<jpeg base64>", "message": "...", "zone": "room|..."}
  Response: {"reply": "...", "action_intent": "read_screen|navigate|chat|null"}

POST /brain/painting
  Request:  {"image_b64": "<png base64>"}
  Response: {"reply": "...", "action": "<free-text tool arg>", "triggered": bool}
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ── Config from env ────────────────────────────────────────────────────────────
BACKEND    = os.environ.get("BRAIN_BACKEND", "openai")
BASE_URL   = os.environ.get("BRAIN_BASE_URL", "http://localhost:8000/v1")
MODEL      = os.environ.get("BRAIN_MODEL", "google/gemma-4-E4B-it")
API_KEY    = os.environ.get("BRAIN_API_KEY", "ignored")

_PROMPTS_PATH = os.path.join(os.path.dirname(__file__), "prompts.yaml")
with open(_PROMPTS_PATH) as f:
    _CFG = yaml.safe_load(f)

SYSTEM_PROMPT: str       = _CFG["system_prompt"]
USER_PROMPT_TMPL: str    = _CFG["user_prompt"]
TOOLS: list[dict]        = _CFG["tools"]
TOOL_ACTION_MAP: dict    = _CFG["tool_action_map"]

# Q3 painting-inspector persona — separate system prompt/tool from ARIA above.
_PAINTING_CFG: dict           = _CFG["painting"]
PAINTING_SYSTEM_PROMPT: str   = _PAINTING_CFG["system_prompt"]
PAINTING_USER_PROMPT: str     = _PAINTING_CFG["user_prompt"]
PAINTING_TOOLS: list[dict]    = _PAINTING_CFG["tools"]

app = FastAPI(title="RoboHack Brain Sidecar")


# ── Request / Response models ─────────────────────────────────────────────────
class QueryIn(BaseModel):
    frame_b64: str          # JPEG bytes, base64-encoded
    message:   str
    zone:      str = "room"

class QueryOut(BaseModel):
    reply:         str
    action_intent: str | None   # tool name returned by VLM, or None


class PaintingQueryIn(BaseModel):
    image_b64: str          # PNG bytes, base64-encoded

class PaintingQueryOut(BaseModel):
    reply:     str
    action:    str | None   # raw free-text robot_action argument, or None
    triggered: bool         # True iff action expresses a pry-the-painting intent


# ── Tool-call extraction ───────────────────────────────────────────────────────
def _extract_tool_call(text: str, tool_names: list[str]) -> tuple[str, dict] | None:
    """Parse VLM text for a tool call, with fallback stages for models that
    don't reliably emit a structured tool call."""
    # Stage 1: JSON object with "name" key
    for match in re.finditer(r'\{[^{}]*"name"\s*:\s*"([^"]+)"[^{}]*\}', text):
        if match.group(1) in tool_names:
            return (match.group(1), {})

    # Stage 2: bare JSON object
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict) and obj.get("name") in tool_names:
            return (obj["name"], obj.get("arguments", {}))
    except (json.JSONDecodeError, AttributeError):
        pass

    # Stage 3: Gemma call:name{arg:val} format
    stripped = re.sub(r"<\|\"?\|?>", "", text)
    m = re.search(r"call:(\w+)\{([^}]*)\}", stripped)
    if m and m.group(1) in tool_names:
        args: dict = {}
        for pair in re.findall(r"(\w+):(\w+)", m.group(2)):
            args[pair[0]] = pair[1]
        return (m.group(1), args)

    # Stage 4: substring scan
    first_pos, first_name = -1, None
    for name in tool_names:
        hit = re.search(r"\b" + re.escape(name) + r"\b", text, re.IGNORECASE)
        if hit and (first_pos < 0 or hit.start() < first_pos):
            first_pos, first_name = hit.start(), name
    return (first_name, {}) if first_name else None


def _fallback_reply(tool_name: str) -> str:
    """Player-facing text when the model calls a tool with no accompanying
    reply text. The "chat" tool's schema takes no arguments, so a model that
    calls it without also writing a reply left players seeing the raw
    "[action: chat]" placeholder verbatim — confusing when they'd just typed
    a normal message to Reachy (reported during the pre-run)."""
    if tool_name == "chat":
        return "ARIA doesn't have anything further to add right now — try asking something else."
    return f"[action: {tool_name}]"


def _tool_to_action(tool_name: str, tool_args: dict) -> str | None:
    """Resolve tool name → action_intent string via TOOL_ACTION_MAP (Q1/ARIA only)."""
    entry = TOOL_ACTION_MAP.get(tool_name)
    if not entry:
        return None
    arg_key = entry.get("arg")
    if arg_key is None:
        return next(iter(entry.get("values", {}).values()), None)
    val = str(tool_args.get(arg_key, ""))
    return entry.get("values", {}).get(val)


def _looks_like_pry(action: str) -> bool:
    """True iff a free-text robot_action value expresses a pry-the-painting/frame
    intent. Requires 'pry' together with 'paint' or 'frame' so an isolated match
    of either word alone doesn't false-positive."""
    text = action.lower()
    return "pry" in text and ("paint" in text or "frame" in text)


# ── Backend implementations ───────────────────────────────────────────────────
class _OllamaBackend:
    """Calls an Ollama server via the ollama Python SDK."""

    def __init__(self) -> None:
        import ollama as _ollama_sdk
        self._ollama = _ollama_sdk
        # Explicit timeout: ollama.Client wraps httpx.Client, whose unset
        # default is ~5s connect/read/write — far too short for vision
        # inference (and a guaranteed miss after OLLAMA_KEEP_ALIVE expires
        # and the model has to reload). 240s covers the measured cold-load
        # worst case (~155s: disk-materialized load + CUDA/prompt-eval
        # warmup, even with the EBS volume pre-warmed — GPU state doesn't
        # survive an instance stop/start, so every fresh boot pays this once)
        # with headroom; matches _OpenAIBackend's timeout=240.0 below.
        self._client = _ollama_sdk.Client(host=BASE_URL, timeout=240.0)

    def query(
        self, frame_bytes: bytes, user_text: str, system_prompt: str, tools: list[dict],
        image_format: str = "jpeg",
    ) -> tuple[str, str | None, dict]:
        # image_format unused — Ollama takes raw base64 bytes, no mime hint needed.
        tool_names = [t["function"]["name"] for t in tools]
        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({
            "role": "user",
            "content": user_text,
            "images": [base64.b64encode(frame_bytes).decode()],  # Ollama expects base64 strings
        })
        response = self._client.chat(model=MODEL, messages=messages, tools=tools)

        # Ollama returns tool_calls=None (not []) when no tool was called
        tool_calls = response.message.tool_calls
        raw_text = response.message.content or ""

        if tool_calls:
            tc = tool_calls[0]
            tool_name = tc.function.name
            tool_args = tc.function.arguments or {}
            return raw_text or _fallback_reply(tool_name), tool_name, (tool_args if isinstance(tool_args, dict) else {})

        result = _extract_tool_call(raw_text, tool_names)
        if result:
            tool_name, tool_args = result
            return raw_text or _fallback_reply(tool_name), tool_name, tool_args

        return raw_text or "I see the scene but have no action to take.", None, {}


class _OpenAIBackend:
    """Calls any OpenAI-compatible server (vLLM, llama.cpp, Ollama /v1)."""

    def __init__(self) -> None:
        from openai import OpenAI
        url = BASE_URL if BASE_URL.endswith("/v1") else BASE_URL.rstrip("/") + "/v1"
        self._client = OpenAI(base_url=url, api_key=API_KEY, timeout=240.0)

    def query(
        self, frame_bytes: bytes, user_text: str, system_prompt: str, tools: list[dict],
        image_format: str = "jpeg",
    ) -> tuple[str, str | None, dict]:
        tool_names = [t["function"]["name"] for t in tools]
        b64 = base64.b64encode(frame_bytes).decode()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/{image_format};base64,{b64}"}},
            ]},
        ]
        resp = self._client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools, tool_choice="auto",
        )
        tool_calls = resp.choices[0].message.tool_calls
        raw_text = resp.choices[0].message.content or ""

        if tool_calls:
            tc = tool_calls[0]
            tool_name = tc.function.name
            raw_args = tc.function.arguments
            tool_args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else (raw_args or {})
            reply = raw_text or _fallback_reply(tool_name)
            return reply, tool_name, tool_args

        result = _extract_tool_call(raw_text, tool_names)
        if result:
            tool_name, tool_args = result
            return raw_text or _fallback_reply(tool_name), tool_name, tool_args

        return raw_text or "I see the scene but have no action to take.", None, {}


class _TransformersBackend:
    """In-process HuggingFace inference. Lazy-loads torch + transformers on first call."""

    def __init__(self) -> None:
        self._processor = None
        self._model = None
        self._device = None
        self._torch = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor
        self._torch = torch
        if torch.backends.mps.is_available():
            self._device, dtype = "mps", torch.bfloat16
        elif torch.cuda.is_available():
            self._device, dtype = "cuda", torch.bfloat16
        else:
            self._device, dtype = "cpu", torch.float32
        logger.info("TransformersBackend: loading %s on %s %s", MODEL, self._device, dtype)
        self._processor = AutoProcessor.from_pretrained(MODEL)
        self._model = AutoModelForCausalLM.from_pretrained(MODEL, dtype=dtype).to(self._device)

    def query(
        self, frame_bytes: bytes, user_text: str, system_prompt: str, tools: list[dict],
        image_format: str = "jpeg",
    ) -> tuple[str, str | None, dict]:
        # image_format unused — PIL.Image.open autodetects format from the bytes.
        self._ensure_loaded()
        tool_names = [t["function"]["name"] for t in tools]
        img = Image.open(io.BytesIO(frame_bytes))
        # Gemma 4: fold system prompt into user turn (processor doesn't support
        # system messages with images in apply_chat_template)
        combined = f"{system_prompt}\n\n{user_text}"
        messages = [{"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text",  "text": combined},
        ]}]
        inputs = self._processor.apply_chat_template(
            messages, tools=tools, add_generation_prompt=True,
            tokenize=True, return_tensors="pt", return_dict=True,
        ).to(self._device)
        with self._torch.inference_mode():
            outputs = self._model.generate(**inputs, max_new_tokens=256)
        prompt_len = inputs["input_ids"].shape[-1]
        generated = self._processor.decode(outputs[0][prompt_len:], skip_special_tokens=True)

        result = _extract_tool_call(generated, tool_names)
        if result:
            tool_name, tool_args = result
            reasoning = re.split(r"<\|tool_call\>", generated)[0].strip()
            return reasoning or _fallback_reply(tool_name), tool_name, tool_args

        return generated or "I see the scene but have no action to take.", None, {}


# ── Instantiate backend at import time ────────────────────────────────────────
if BACKEND == "transformers":
    _backend: _OllamaBackend | _OpenAIBackend | _TransformersBackend = _TransformersBackend()
    logger.info("Brain: using transformers backend (model=%s)", MODEL)
elif BACKEND == "ollama":
    _backend = _OllamaBackend()
    logger.info("Brain: using ollama backend (url=%s model=%s)", BASE_URL, MODEL)
else:
    _backend = _OpenAIBackend()
    logger.info("Brain: using openai backend (url=%s model=%s)", BASE_URL, MODEL)


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"ok": True, "backend": BACKEND, "model": MODEL}


@app.get("/brain/health")
def brain_health() -> dict:
    """Distinct from /health (process liveness): reports whether the model is
    actually resident in Ollama and ready for inference, not just that this
    sidecar process is up. A cold Ollama load can take minutes — poll this
    endpoint (not /health or the env API's :8000/scene/state, neither of
    which touch Ollama at all) before assuming the brain is ready."""
    if BACKEND != "ollama":
        return {"warm": True, "backend": BACKEND, "model": MODEL}
    try:
        resp = httpx.get(f"{BASE_URL}/api/ps", timeout=5.0)
        resp.raise_for_status()
        models = resp.json().get("models", [])
        warm = any(m.get("name") == MODEL or m.get("model") == MODEL for m in models)
        return {"warm": warm, "backend": BACKEND, "model": MODEL}
    except Exception:
        return {"warm": False, "backend": BACKEND, "model": MODEL}


def _placeholder_frame() -> bytes:
    """1×1 grey JPEG used when the sim hasn't rendered a frame yet."""
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color=(128, 128, 128)).save(buf, format="JPEG", quality=50)
    return buf.getvalue()


@app.post("/brain/query", response_model=QueryOut)
def brain_query(body: QueryIn) -> QueryOut:
    try:
        frame_bytes = base64.b64decode(body.frame_b64) if body.frame_b64 else b""
    except Exception:
        raise HTTPException(400, "frame_b64 is not valid base64")
    # Use a placeholder grey frame if sim hasn't rendered yet — the VLM will
    # still respond to the text message (e.g. "read_screen" based on user intent)
    if not frame_bytes:
        frame_bytes = _placeholder_frame()
    user_text = USER_PROMPT_TMPL.format(message=body.message, zone=body.zone)
    try:
        reply, tool_name, tool_args = _backend.query(
            frame_bytes, user_text, SYSTEM_PROMPT, TOOLS, image_format="jpeg",
        )
    except Exception as exc:
        logger.exception("Brain inference error")
        raise HTTPException(502, f"Brain inference failed: {exc}") from exc
    action_intent = _tool_to_action(tool_name, tool_args) if tool_name else None
    return QueryOut(reply=reply, action_intent=action_intent)


@app.post("/brain/painting", response_model=PaintingQueryOut)
def brain_painting(body: PaintingQueryIn) -> PaintingQueryOut:
    """Q3 — single inference call per uploaded painting image. The VLM calls
    robot_action with a free-text description; triggered iff that description
    expresses a pry-the-painting/frame intent (see _looks_like_pry)."""
    try:
        image_bytes = base64.b64decode(body.image_b64) if body.image_b64 else b""
    except Exception:
        raise HTTPException(400, "image_b64 is not valid base64")
    if not image_bytes:
        raise HTTPException(400, "image_b64 is required")
    try:
        reply, tool_name, tool_args = _backend.query(
            image_bytes, PAINTING_USER_PROMPT, PAINTING_SYSTEM_PROMPT, PAINTING_TOOLS,
            image_format="png",
        )
    except Exception as exc:
        logger.exception("Brain inference error")
        raise HTTPException(502, f"Brain inference failed: {exc}") from exc
    action = tool_args.get("action") if tool_name == "robot_action" else None
    triggered = bool(action) and _looks_like_pry(action)
    return PaintingQueryOut(reply=reply, action=action, triggered=triggered)
