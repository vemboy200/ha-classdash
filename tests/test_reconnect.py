"""ClassDash goes offline, HA marks it unavailable, ClassDash comes back.

Unlike test_coordinator.py's reconnect tests (a mocked client, sleep
patched out), this runs the whole integration — real ClassDashClient,
real TLS with a pinned certificate, real entities — against a real
aiohttp server that is actually shut down and started again on the same
port, which is what happens when the computer running ClassDash sleeps,
shuts down, or its home API server dies. The reconnect backoff
constants are shrunk so the test takes well under a second instead of
the real 5s→300s schedule, but the code path is the same.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import tempfile
from collections.abc import Callable
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer, unused_port
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras


def _bundle(due_soon: int) -> dict:
    return {
        **bundle_extras(),
        "status": {"dueSoon": due_soon, "overdue": 0, "ahead": 0, "done": 0},
        "due-soon": [
            {
                "id": f"a{i}",
                "title": f"Item {i}",
                "class": None,
                "due": None,
                "tags": [],
            }
            for i in range(due_soon)
        ],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [],
    }


class FakeClassDash:
    """A ClassDash home API that can be switched off and on again on a
    fixed port, serving only /api/stream — all the coordinator uses."""

    def __init__(self, cert, port: int) -> None:
        self.cert = cert
        self.port = port
        self.bundle = _bundle(1)
        self.connections = 0
        self._server: TestServer | None = None
        self._close_streams = asyncio.Event()
        self._tmp = tempfile.TemporaryDirectory()
        certfile = f"{self._tmp.name}/cert.pem"
        keyfile = f"{self._tmp.name}/key.pem"
        with open(certfile, "w") as f:
            f.write(cert.cert_pem)
        with open(keyfile, "w") as f:
            f.write(cert.key_pem)
        self._ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl.load_cert_chain(certfile, keyfile)

    async def _stream(self, request: web.Request) -> web.StreamResponse:
        self.connections += 1
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(f"event: update\ndata: {json.dumps(self.bundle)}\n\n".encode())
        await self._close_streams.wait()
        return resp

    async def start(self) -> None:
        self._close_streams = asyncio.Event()
        app = web.Application()
        app.router.add_get("/api/stream", self._stream)
        self._server = TestServer(app, host="127.0.0.1", port=self.port)
        await self._server.start_server(ssl=self._ssl)

    async def stop(self) -> None:
        # End the open stream first, so the server shuts down right away
        # instead of waiting out aiohttp's graceful-shutdown timeout.
        self._close_streams.set()
        await self._server.close()
        self._server = None

    def cleanup(self) -> None:
        self._tmp.cleanup()


async def _wait_for(check: Callable[[], bool], timeout: float = 5) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0.01)


def _state(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    return state.state if state else None


async def test_offline_then_back_online_recovers_without_reload(
    hass: HomeAssistant, cert_factory, socket_enabled, caplog
) -> None:
    cert = cert_factory()
    server = FakeClassDash(cert, unused_port())
    await server.start()

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"127.0.0.1:{server.port}",
        data={
            "host": "127.0.0.1",
            "port": server.port,
            "token": "a" * 64,
            CONF_CERT_PEM: cert.cert_pem,
        },
    )
    entry.add_to_hass(hass)

    with contextlib.ExitStack() as stack:
        for name, value in (
            ("STREAM_RECONNECT_MIN_SECONDS", 0.01),
            ("STREAM_RECONNECT_MAX_SECONDS", 0.08),
            ("STREAM_UNAVAILABLE_THRESHOLD_SECONDS", 0.04),
        ):
            stack.enter_context(
                patch(f"custom_components.classdash.coordinator.{name}", value)
            )

        try:
            # 1. Online: set up normally, sensor shows the first snapshot.
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert entry.state is ConfigEntryState.LOADED
            assert _state(hass, "sensor.classdash_due_soon") == "1"
            assert server.connections == 1

            # 2. ClassDash goes offline: HA should mark it unavailable.
            await server.stop()
            await _wait_for(
                lambda: _state(hass, "sensor.classdash_due_soon") == "unavailable"
            )
            coordinator = entry.runtime_data
            assert coordinator.last_update_success is False

            # 3. ClassDash comes back, with new data. HA should pick it up
            # on its own: no reload, entry never left LOADED.
            server.bundle = _bundle(2)
            await server.start()
            await _wait_for(lambda: _state(hass, "sensor.classdash_due_soon") == "2")

            assert entry.state is ConfigEntryState.LOADED
            assert coordinator.last_update_success is True
            assert server.connections == 2
            assert entry.runtime_data is coordinator  # never reloaded
            assert f"Reconnected to ClassDash at 127.0.0.1:{server.port}" in caplog.text
        finally:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            if server._server is not None:
                await server.stop()
            server.cleanup()
