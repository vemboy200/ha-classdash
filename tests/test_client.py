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


async def test_async_get_update_status_success(cert_factory, socket_enabled) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://example.com/release",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }

    async def update_status(request: web.Request) -> web.Response:
        return web.json_response(status)

    app = web.Application()
    app.router.add_get("/api/update-status", update_status)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            result = await client.async_get_update_status()

    assert result == status


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


async def test_async_download_update_posts_with_no_body(
    cert_factory, socket_enabled
) -> None:
    received: dict = {}

    async def download(request: web.Request) -> web.Response:
        received["method"] = request.method
        received["body"] = await request.read()
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/api/update-status/download", download)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            assert await client.async_download_update() is None

    assert received == {"method": "POST", "body": b""}


async def test_async_get_update_status_401_raises_auth_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_get("/api/update-status", _unauthorized)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashAuthError):
                await client.async_get_update_status()


async def test_async_download_update_server_error_raises_connection_error(
    cert_factory, socket_enabled
) -> None:
    app = web.Application()
    app.router.add_post("/api/update-status/download", _server_error)

    async with _running_app(cert_factory, app) as (cert, port):
        async with ClientSession() as session:
            client = _client_for(cert, port, session)
            with pytest.raises(ClassDashConnectionError):
                await client.async_download_update()


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
