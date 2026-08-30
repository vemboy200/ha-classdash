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
        {"status": {"dueSoon": 1}},
        {"status": {"dueSoon": 2}},
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


async def test_async_stream_updates_ignores_non_update_events(
    cert_factory, socket_enabled
) -> None:
    """Only `event: update` payloads are yielded — a comment/keepalive
    line or a differently-named event shouldn't produce a bogus item."""
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

    assert events == [{"status": {"dueSoon": 5}}]
