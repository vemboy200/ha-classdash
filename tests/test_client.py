"""Tests for ClassDashClient against a real HTTPS server.

Everywhere else, ClassDashClient itself is mocked away — these are the
only tests that exercise its actual HTTP/SSE parsing against a genuine
aiohttp server (via aiohttp's own TestServer, TLS-wrapped with a
throwaway certificate), rather than trusting it by construction.
"""

from __future__ import annotations

import contextlib
import ssl
import tempfile
from collections.abc import AsyncIterator

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashClient,
    ClassDashConnectionError,
    ClassDashValidationError,
    StreamEvent,
    build_ssl_context,
)


@contextlib.asynccontextmanager
async def _running_app(cert_factory, app: web.Application) -> AsyncIterator[int]:
    """Serve `app` over TLS on an ephemeral port, torn down on exit."""
    cert = cert_factory()
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with (
        tempfile.NamedTemporaryFile("w", suffix=".pem") as certfile,
        tempfile.NamedTemporaryFile("w", suffix=".pem") as keyfile,
    ):
        certfile.write(cert.cert_pem)
        certfile.flush()
        keyfile.write(cert.key_pem)
        keyfile.flush()
        server_ctx.load_cert_chain(certfile.name, keyfile.name)

        server = TestServer(app, port=0)
        await server.start_server(ssl=server_ctx)
        try:
            yield cert, server.port
        finally:
            await server.close()


def _client_for(cert, port: int, session: ClientSession, token: str = "t") -> ClassDashClient:
    return ClassDashClient(
        session, "127.0.0.1", port, token, build_ssl_context(cert.cert_pem)
    )


async def _unauthorized(request: web.Request) -> web.Response:
    return web.json_response({"error": "no"}, status=401)


async def _server_error(request: web.Request) -> web.Response:
    return web.Response(status=500)


async def test_async_get_status_success(cert_factory, socket_enabled) -> None:
    received_auth = None

    async def status(request: web.Request) -> web.Response:
        nonlocal received_auth
        received_auth = request.headers.get("Authorization")
        return web.json_response({"dueSoon": 3})

    app = web.Application()
    app.router.add_get("/api/status", status)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session, token="secret-token")
            result = await client.async_get_status()

    assert result == {"dueSoon": 3}
    assert received_auth == "Bearer secret-token"


async def test_async_get_status_401_raises_auth_error(cert_factory, socket_enabled) -> None:
    app = web.Application()
    app.router.add_get("/api/status", _unauthorized)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashAuthError):
                await client.async_get_status()


async def test_async_get_status_server_error_raises_connection_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_get("/api/status", _server_error)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashConnectionError):
                await client.async_get_status()


async def test_async_reload_posts_and_returns_nothing(
    cert_factory, socket_enabled
) -> None:
    received: dict[str, str] = {}

    async def reload(request: web.Request) -> web.Response:
        received["method"] = request.method
        received["auth"] = request.headers.get("Authorization")
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/api/reload", reload)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session, token="secret-token")
            assert await client.async_reload() is None

    assert received == {"method": "POST", "auth": "Bearer secret-token"}


async def _ok(request: web.Request) -> web.Response:
    return web.Response(status=200)


async def test_async_check_posts_and_returns_nothing(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_post("/api/check", _ok)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            assert await client.async_check() is None


async def test_async_reload_401_raises_auth_error(cert_factory, socket_enabled) -> None:
    app = web.Application()
    app.router.add_post("/api/reload", _unauthorized)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashAuthError):
                await client.async_reload()


async def test_async_check_server_error_raises_connection_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_post("/api/check", _server_error)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashConnectionError):
                await client.async_check()


async def test_async_hide_posts_id_in_body(cert_factory, socket_enabled) -> None:
    received: dict = {}

    async def hide(request: web.Request) -> web.Response:
        received["body"] = await request.json()
        received["auth"] = request.headers.get("Authorization")
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/api/hide", hide)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session, token="secret-token")
            assert await client.async_hide("abc123") is None

    assert received == {"body": {"id": "abc123"}, "auth": "Bearer secret-token"}


@pytest.mark.parametrize(
    ("method_name", "path"),
    [
        ("async_unhide", "/api/unhide"),
        ("async_mute", "/api/mute"),
        ("async_unmute", "/api/unmute"),
    ],
)
async def test_write_methods_post_id_to_the_right_path(
    cert_factory, socket_enabled, method_name, path
) -> None:
    received: dict = {}

    async def handler(request: web.Request) -> web.Response:
        received["body"] = await request.json()
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post(path, handler)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            assert await getattr(client, method_name)("x1") is None

    assert received == {"body": {"id": "x1"}}


async def test_async_hide_401_raises_auth_error(cert_factory, socket_enabled) -> None:
    app = web.Application()
    app.router.add_post("/api/hide", _unauthorized)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashAuthError):
                await client.async_hide("x1")


async def test_async_mute_server_error_raises_connection_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_post("/api/mute", _server_error)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashConnectionError):
                await client.async_mute("x1")


async def test_async_dismiss_update_posts_version_in_body(
    cert_factory, socket_enabled
) -> None:
    received: dict = {}

    async def dismiss(request: web.Request) -> web.Response:
        received["body"] = await request.json()
        received["auth"] = request.headers.get("Authorization")
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/api/update-status/dismiss", dismiss)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session, token="secret-token")
            assert await client.async_dismiss_update("0.4.0") is None

    assert received == {"body": {"version": "0.4.0"}, "auth": "Bearer secret-token"}


async def test_async_get_virtual_success(cert_factory, socket_enabled) -> None:
    reminders = [{"id": "v-1", "title": "Bring slip", "class": "Physics", "due": None}]

    async def virtual(request: web.Request) -> web.Response:
        return web.json_response(reminders)

    app = web.Application()
    app.router.add_get("/api/virtual", virtual)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            result = await client.async_get_virtual()

    assert result == reminders


async def test_async_create_virtual_reminder_posts_and_returns_entry(
    cert_factory, socket_enabled
) -> None:
    received: dict = {}
    entry = {
        "id": "v-abc123",
        "title": "Bring slip",
        "class": "Physics",
        "due": "2026-09-10T14:30:00.000Z",
    }

    async def create(request: web.Request) -> web.Response:
        received["body"] = await request.json()
        received["auth"] = request.headers.get("Authorization")
        return web.json_response({"ok": True, "entry": entry})

    app = web.Application()
    app.router.add_post("/api/virtual/create", create)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session, token="secret-token")
            result = await client.async_create_virtual_reminder(
                "Bring slip", "Physics", "2026-09-10T14:30:00+00:00"
            )

    assert result == {"ok": True, "entry": entry}
    assert received == {
        "body": {
            "title": "Bring slip",
            "class": "Physics",
            "due": "2026-09-10T14:30:00+00:00",
        },
        "auth": "Bearer secret-token",
    }


async def test_async_create_virtual_reminder_omits_class_and_due(
    cert_factory, socket_enabled
) -> None:
    received: dict = {}

    async def create(request: web.Request) -> web.Response:
        received["body"] = await request.json()
        return web.json_response({"ok": True, "entry": {}})

    app = web.Application()
    app.router.add_post("/api/virtual/create", create)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            await client.async_create_virtual_reminder("General reminder")

    assert received["body"] == {"title": "General reminder", "class": None, "due": None}


async def test_async_edit_virtual_reminder_posts_full_replacement(
    cert_factory, socket_enabled
) -> None:
    received: dict = {}
    entry = {"id": "v-abc123", "title": "Edited", "class": None, "due": None}

    async def edit(request: web.Request) -> web.Response:
        received["body"] = await request.json()
        return web.json_response({"ok": True, "entry": entry})

    app = web.Application()
    app.router.add_post("/api/virtual/edit", edit)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            result = await client.async_edit_virtual_reminder("v-abc123", "Edited")

    assert result == {"ok": True, "entry": entry}
    assert received["body"] == {
        "id": "v-abc123",
        "title": "Edited",
        "class": None,
        "due": None,
    }


async def test_async_create_virtual_reminder_400_raises_validation_error(
    cert_factory, socket_enabled
) -> None:
    async def create(request: web.Request) -> web.Response:
        return web.json_response({"ok": False, "why": "title is required"}, status=400)

    app = web.Application()
    app.router.add_post("/api/virtual/create", create)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashValidationError, match="title is required"):
                await client.async_create_virtual_reminder("")


async def test_async_edit_virtual_reminder_400_error_shape_also_handled(
    cert_factory, socket_enabled
) -> None:
    """The malformed-request-shape 400s (missing id entirely) use an
    "error" key instead of "why" — both shapes need to surface a real
    message, not a blank one."""

    async def edit(request: web.Request) -> web.Response:
        return web.json_response(
            {"error": 'expected a JSON body: {"id": "..."}'}, status=400
        )

    app = web.Application()
    app.router.add_post("/api/virtual/edit", edit)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashValidationError, match="expected a JSON body"):
                await client.async_edit_virtual_reminder("", "Edited")


async def test_async_get_virtual_401_raises_auth_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_get("/api/virtual", _unauthorized)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashAuthError):
                await client.async_get_virtual()


async def test_async_create_virtual_reminder_server_error_raises_connection_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_post("/api/virtual/create", _server_error)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashConnectionError):
                await client.async_create_virtual_reminder("Bring slip")


async def test_async_stream_updates_yields_parsed_events(
    cert_factory, socket_enabled
) -> None:
    body = (
        b"event: update\ndata: {\"status\": {\"dueSoon\": 1}}\n\n"
        b"event: update\ndata: {\"status\": {\"dueSoon\": 2}}\n\n"
    )

    async def stream(request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="text/event-stream")

    app = web.Application()
    app.router.add_get("/api/stream", stream)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            events = [e async for e in client.async_stream_updates()]

    assert events == [
        StreamEvent("update", {"status": {"dueSoon": 1}}),
        StreamEvent("update", {"status": {"dueSoon": 2}}),
    ]


async def test_async_stream_updates_yields_heartbeats_too(
    cert_factory, socket_enabled
) -> None:
    """Heartbeats are a distinct, real event type the client surfaces —
    not something it should filter out as noise."""
    body = (
        b"event: update\ndata: {\"status\": {\"dueSoon\": 1}}\n\n"
        b"event: heartbeat\ndata: {\"status\": {\"dueSoon\": 1, \"minutesAgo\": 2}}\n\n"
    )

    async def stream(request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="text/event-stream")

    app = web.Application()
    app.router.add_get("/api/stream", stream)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            events = [e async for e in client.async_stream_updates()]

    assert events == [
        StreamEvent("update", {"status": {"dueSoon": 1}}),
        StreamEvent("heartbeat", {"status": {"dueSoon": 1, "minutesAgo": 2}}),
    ]


async def test_async_stream_updates_401_raises_auth_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_get("/api/stream", _unauthorized)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashAuthError):
                async for _ in client.async_stream_updates():
                    pass


async def test_async_stream_updates_ignores_unknown_events(
    cert_factory, socket_enabled
) -> None:
    """Only `update` and `heartbeat` payloads are yielded — a
    comment/keepalive line or an event name that's neither shouldn't
    produce a bogus item."""
    body = (
        b": keepalive\n\n"
        b"event: other\ndata: {\"ignored\": true}\n\n"
        b"event: update\ndata: {\"status\": {\"dueSoon\": 5}}\n\n"
    )

    async def stream(request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="text/event-stream")

    app = web.Application()
    app.router.add_get("/api/stream", stream)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            events = [e async for e in client.async_stream_updates()]

    assert events == [StreamEvent("update", {"status": {"dueSoon": 5}})]
