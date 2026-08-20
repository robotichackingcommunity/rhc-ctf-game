"""Close-handling tests for WS /ws/cameras.

Reproduce the code-1006 drops seen on the live fleet. The send loop never reads
from the socket, so it only discovers a dead connection on its next send — where
uvicorn raises RuntimeError("Unexpected ASGI message 'websocket.send', after
sending 'websocket.close'"). The handler catches only WebSocketDisconnect, so
that RuntimeError escapes and the client sees an abnormal 1006 teardown.

What actually kills the connection in production is uvicorn's own WS keepalive
(ping 20s / pong timeout 20s by default) firing while the event loop is starved,
NOT a peer close — so the server fixture below runs deliberately tight ping
settings and one test drives a client that never answers a ping.

Runs a real uvicorn server against real clients on purpose: the failure is in the
ASGI/websockets transport, and starlette's TestClient (an in-memory transport
that silently swallows sends after a close) cannot see it.

Drives the real sim frame buffer — the same dict the render worker writes — but
never starts the sim thread.
"""
import asyncio
import base64
import logging
import os
import socket
import threading
import time

import pytest
import uvicorn
import websockets

import app as env_app
import sim as _sim

PORT = 8123            # not 8000: a local Chrome tends to squat on that
TOKEN = env_app.STATE["team_token"]
PUBLIC_CAM = "reachy_pov"       # ?cams= name
CAM = "eye_camera"              # the internal camera it maps to
WS_PATH = f"/ws/cameras?token={TOKEN}&cams={PUBLIC_CAM}"
URL = f"ws://127.0.0.1:{PORT}{WS_PATH}"

# Tight enough to make the keepalive path testable in seconds; still slack enough
# that a cooperative client which pongs normally is never killed spuriously.
PING_INTERVAL = 0.5
PING_TIMEOUT = 2.0


@pytest.fixture(scope="module")
def server():
    """One server for the module — app.py's module-level asyncio.Event binds to the
    first loop that touches it, exactly as it does in production."""
    config = uvicorn.Config(env_app.app, host="127.0.0.1", port=PORT, log_level="warning",
                            ws_ping_interval=PING_INTERVAL, ws_ping_timeout=PING_TIMEOUT)
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while not srv.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert srv.started, "uvicorn did not start"
    yield srv
    srv.should_exit = True
    t.join(timeout=10)


@pytest.fixture
def asgi_errors():
    """Collect anything uvicorn logs while a test runs — an escaping RuntimeError
    surfaces there as an ASGI exception."""
    records: list[str] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Collector()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


@pytest.fixture(autouse=True)
def clean_camera_state():
    yield
    with _sim.frame_lock:
        _sim.latest_frames.pop(CAM, None)
    with _sim.active_lock:
        _sim._camera_refcount.clear()


@pytest.fixture
def frame_pump():
    """Stand in for the render worker: keep publishing *changed* bytes for CAM so
    the send loop always has something to send on its next pass."""
    stop = threading.Event()

    def pump():
        n = 0
        while not stop.is_set():
            with _sim.frame_lock:
                _sim.latest_frames[CAM] = f"frame-{n}".encode()
            n += 1
            time.sleep(0.01)

    t = threading.Thread(target=pump, daemon=True)
    t.start()
    yield
    stop.set()
    t.join(timeout=2)


def _wait_unsubscribed(timeout: float) -> bool:
    """True once the handler dropped its camera subscription — i.e. it noticed the
    connection was gone and ran its finally."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _sim.active_lock:
            if not _sim._camera_refcount.get(CAM):
                return True
        time.sleep(0.05)
    return False


def _asgi_failures(records: list[str]) -> list[str]:
    return [r for r in records
            if "Unexpected ASGI message" in r or "Exception in ASGI application" in r]


def _silent_ws_handshake() -> socket.socket:
    """Complete a WS handshake by hand, then go silent — never answering uvicorn's
    keepalive pings. This is how the connection dies in production: uvicorn's own
    ping timeout closes it, and the app's next send lands after that close."""
    key = base64.b64encode(os.urandom(16)).decode()
    req = (f"GET {WS_PATH} HTTP/1.1\r\n"
           f"Host: 127.0.0.1:{PORT}\r\n"
           "Upgrade: websocket\r\n"
           "Connection: Upgrade\r\n"
           f"Sec-WebSocket-Key: {key}\r\n"
           "Sec-WebSocket-Version: 13\r\n\r\n")
    s = socket.create_connection(("127.0.0.1", PORT), timeout=10)
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            break
        buf += chunk
    assert b"101" in buf.split(b"\r\n")[0], f"handshake failed: {buf[:120]!r}"
    return s


def test_keepalive_timeout_close_is_not_an_asgi_error(server, asgi_errors, frame_pump):
    """The production failure: uvicorn's pong deadline passes, uvicorn closes the
    connection, and the send loop's next send lands after that close. That must end
    the handler cleanly instead of raising out of it."""
    sock = _silent_ws_handshake()
    try:
        time.sleep(PING_INTERVAL + PING_TIMEOUT + 2.0)
    finally:
        sock.close()

    assert _wait_unsubscribed(5.0), "handler never released its subscription"
    blew_up = _asgi_failures(asgi_errors)
    assert not blew_up, f"keepalive close raised out of the handler: {blew_up}"


def test_peer_close_mid_stream_is_not_an_asgi_error(server, asgi_errors, frame_pump):
    """A client closing while frames still flow must also end the handler cleanly."""
    async def scenario():
        async with websockets.connect(URL) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
        # __aexit__ closes; give the server a pass or two to try another send.
        await asyncio.sleep(1.0)

    asyncio.run(scenario())
    assert _wait_unsubscribed(5.0), "handler never released its subscription"
    blew_up = _asgi_failures(asgi_errors)
    assert not blew_up, f"peer close raised out of the handler: {blew_up}"


def test_close_noticed_even_when_no_further_frames_arrive(server):
    """With the feed idle (no changed bytes) the send loop never sends again, so the
    close is never discovered and the subscription leaks. The handler has to read
    the socket to learn about it."""
    with _sim.frame_lock:
        _sim.latest_frames[CAM] = b"one-and-only-frame"

    async def scenario():
        async with websockets.connect(URL) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
            with _sim.active_lock:
                assert _sim._camera_refcount.get(CAM) == 1

    asyncio.run(scenario())
    assert _wait_unsubscribed(5.0), "handler never noticed the close"
