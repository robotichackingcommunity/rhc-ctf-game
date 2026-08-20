"""The sim must not try to open a GL viewer window on a headless server.

The passive viewer costs ~2 of the game server's 4 vCPUs (main thread blocked in
viewer.sync(), plus the viewer's own render thread and llvmpipe workers), and it
throttles the sim tick rate that sets the camera frame rate. Nothing deployed can
ever look at that window, so it has to stay off unless explicitly requested.
"""
import re
from pathlib import Path

import mujoco.viewer
import pytest

import sim as _sim

SIM_SOURCE = Path(_sim.__file__).read_text()


def test_stand_in_covers_every_viewer_attribute_the_sim_loop_uses():
    """The stand-in must implement everything run_sim() calls on the handle.

    Regression test for a real outage: the first version of _NoViewer implemented
    only sync(), so the sim thread died at boot on `while v.is_running():` — the
    API answered but the simulation was dead. Greps the source instead of listing
    attributes by hand, so a newly added viewer call fails here.
    """
    used = set(re.findall(r"\bv\.([a-zA-Z_][a-zA-Z0-9_]*)", SIM_SOURCE))
    used |= set(re.findall(r"\bviewer\.([a-zA-Z_][a-zA-Z0-9_]*)", SIM_SOURCE))
    used.discard("launch_passive")  # that's mujoco.viewer, not the handle

    assert used, "found no viewer attribute uses — the grep is probably broken"
    missing = sorted(a for a in used if not hasattr(_sim._NoViewer(), a))
    assert not missing, f"_NoViewer is missing viewer attribute(s): {missing}"


def test_stand_in_is_running_is_true():
    """Headless has no window to close, so the loop must not exit immediately."""
    assert _sim._NoViewer().is_running() is True


def test_headless_by_default_does_not_launch_a_viewer(monkeypatch):
    """With SIM_VIEWER unset, _viewer_context must not touch mujoco.viewer at all —
    calling launch_passive on a headless box is the thing being avoided."""
    def explode(*a, **k):
        raise AssertionError("launch_passive must not be called when SIM_VIEWER is off")

    monkeypatch.setattr(mujoco.viewer, "launch_passive", explode)
    monkeypatch.setattr(_sim, "_VIEWER_ENABLED", False)

    with _sim._viewer_context(object(), object()) as v:
        assert isinstance(v, _sim._NoViewer)
        assert v, "helpers do `if viewer:` — the stand-in must be truthy"
        assert v.sync() is None


def test_viewer_launched_when_explicitly_enabled(monkeypatch):
    """SIM_VIEWER=1 is the local-desktop path and must still get a real viewer."""
    called = {}

    class FakeHandle:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_launch(m, d):
        called["args"] = (m, d)
        return FakeHandle()

    monkeypatch.setattr(mujoco.viewer, "launch_passive", fake_launch)
    monkeypatch.setattr(_sim, "_VIEWER_ENABLED", True)

    m, d = object(), object()
    handle = _sim._viewer_context(m, d)
    assert called["args"] == (m, d)
    assert isinstance(handle, FakeHandle)


@pytest.mark.parametrize("value,expected", [(None, False), ("0", False), ("1", True)])
def test_flag_parsing(monkeypatch, value, expected):
    """Only an explicit "1" enables it, so a stray empty/garbage value stays off."""
    if value is None:
        monkeypatch.delenv("SIM_VIEWER", raising=False)
    else:
        monkeypatch.setenv("SIM_VIEWER", value)
    import importlib
    reloaded = importlib.reload(_sim)
    try:
        assert reloaded._VIEWER_ENABLED is expected
    finally:
        monkeypatch.delenv("SIM_VIEWER", raising=False)
        importlib.reload(_sim)
