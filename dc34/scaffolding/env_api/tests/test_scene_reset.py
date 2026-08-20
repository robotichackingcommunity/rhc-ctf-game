"""/scene/reset must not claim success it cannot verify.

The endpoint only ENQUEUES a reset; do_reset runs later on the sim thread. So the
old unconditional `{"ok": true, "message": "Simulation and game state reset to
start."}` was wrong in two ways: it was past-tense about work that had not happened
yet, and when the sim thread was dead it was simply false — draining the queue and
mutating STATE both succeed regardless, so a dead sim still answered 200.

That is not hypothetical. It hid a real 20-minute outage on slot 46 today, where
the API answered while the sim thread had died at boot.

These tests pin the three honest outcomes: confirmed, queued-but-unconfirmed, and
sim-is-stalled.
"""
import queue
import threading
import time

import pytest
from starlette.testclient import TestClient

import app as env_app
import sim as _sim

AUTH = {"Authorization": f"Bearer {env_app.STATE['team_token']}"}


@pytest.fixture
def client():
    return TestClient(env_app.app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def clean_sim_state():
    """Each test drives heartbeat/ack itself; leave the module as we found it."""
    saved = dict(_sim.heartbeat)
    yield
    _sim.heartbeat.update(saved)
    for q in (_sim.cmd_queue, _sim.policy_queue, _sim.color_queue, _sim.lock_queue,
              _sim.painting_solve_queue, _sim.screen_unlock_queue):
        while True:
            try:
                q.get_nowait()
            except queue.Empty:
                break
    _sim.abort_event.clear()


def _sim_is_ticking():
    """Pretend the sim thread is alive and stepping."""
    _sim.heartbeat["last_tick"] = time.monotonic()


def _sim_is_stalled():
    """Pretend the sim thread died a while ago."""
    _sim.heartbeat["last_tick"] = time.monotonic() - 10 * env_app._SIM_STALL_SECS


def _ack_after(delay: float):
    """Stand in for the sim thread finishing do_reset."""
    def bump():
        time.sleep(delay)
        _sim.reset_ack["count"] += 1

    t = threading.Thread(target=bump, daemon=True)
    t.start()
    return t


def test_confirmed_reset_reports_success(client):
    """When the sim acknowledges, the past-tense success claim is earned."""
    _sim_is_ticking()
    _ack_after(0.1)

    r = client.post("/scene/reset", headers=AUTH)

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["sim_reset"] is True
    assert "reset to start" in body["message"]


def test_stalled_sim_does_not_claim_the_scene_was_reset(client, monkeypatch):
    """The bug: a dead sim thread used to get a 200 saying the sim was reset."""
    monkeypatch.setattr(env_app, "_RESET_ACK_SECS", 0.3)
    _sim_is_stalled()

    r = client.post("/scene/reset", headers=AUTH)

    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert body["sim_reset"] is False
    assert body["code"] == "sim_stalled"
    assert "NOT" in body["message"], "must say plainly that the scene did not reset"


def test_stalled_sim_still_resets_the_half_we_own(client, monkeypatch):
    """Game-state flags are app-side and always resettable. Reset the half we can,
    and be explicit that the scene half did not happen — resetting nothing would
    make the call non-idempotent for no benefit."""
    monkeypatch.setattr(env_app, "_RESET_ACK_SECS", 0.3)
    env_app.STATE["reachy_unlocked"] = True
    env_app.STATE["robot_zone"] = "restricted"
    _sim_is_stalled()

    client.post("/scene/reset", headers=AUTH)

    assert env_app.STATE["reachy_unlocked"] is False
    assert env_app.STATE["robot_zone"] == "room"


def test_alive_but_slow_sim_reports_pending_not_success(client, monkeypatch):
    """A sim busy unwinding a long command has not reset yet. Saying so beats both
    lying about success and crying sim_stalled at a healthy sim."""
    monkeypatch.setattr(env_app, "_RESET_ACK_SECS", 0.3)
    _sim_is_ticking()  # alive, but nothing will ack

    r = client.post("/scene/reset", headers=AUTH)

    assert r.status_code == 200
    body = r.json()
    assert body["sim_reset"] is False
    assert body["code"] == "reset_pending"


def test_reset_drains_a_queued_policy(client):
    """A Q5 policy queued just before a reset would otherwise start replaying
    against the freshly-reset scene, undoing the reset the player asked for."""
    _sim_is_ticking()
    _sim.policy_queue.put({"actions": [], "fps": 30, "interlock": "engaged"})
    _ack_after(0.1)

    client.post("/scene/reset", headers=AUTH)

    assert _sim.policy_queue.empty(), "queued policy survived the reset"


def test_reset_still_preempts_a_busy_cmd_queue(client):
    """Regression guard on the existing preemption: reset must never 409, even with
    a command already in flight, and must signal the in-flight one to abort."""
    _sim_is_ticking()
    _sim.cmd_queue.put_nowait({"type": "challenge", "q": "q1"})
    _ack_after(0.1)

    r = client.post("/scene/reset", headers=AUTH)

    assert r.status_code == 200
    assert _sim.abort_event.is_set()


def test_reset_requires_auth(client):
    assert client.post("/scene/reset").status_code == 401


@pytest.mark.parametrize("raw,expected", [
    (None, 15.0),
    ("30", 30.0),
    ("abc", 15.0),      # malformed used to crash the env API at import
    ("", 15.0),
])
def test_stall_threshold_survives_a_malformed_value(monkeypatch, raw, expected):
    """float() on a bad SIM_STALL_SECS used to raise at import, taking the whole
    env API down at boot rather than falling back to the default."""
    if raw is None:
        monkeypatch.delenv("SIM_STALL_SECS", raising=False)
    else:
        monkeypatch.setenv("SIM_STALL_SECS", raw)
    assert env_app._float_env("SIM_STALL_SECS", 15.0) == expected
