"""Tests for the certificate-pinning helpers and API client.

These don't touch Home Assistant at all — `test_fetch_and_pin_round_trip`
runs a real, throwaway TLS server (via asyncio + the `cryptography`
library already required by Home Assistant itself) to verify the pinning
actually pins: a client trusting the fetched certificate connects, and one
trusting a different certificate is rejected.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import ssl
import tempfile

import pytest

from custom_components.classdash.api import (
    build_ssl_context,
    fetch_server_certificate,
    fingerprint_from_der,
    pem_from_der,
)

FINGERPRINT_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){31}$")


def test_fingerprint_format(sample_certificate) -> None:
    """32 bytes of SHA-256, colon-separated, uppercase — same as ClassDash prints."""
    assert FINGERPRINT_RE.match(sample_certificate.fingerprint)
    assert fingerprint_from_der(sample_certificate.der) == sample_certificate.fingerprint


def test_pem_from_der_roundtrips(sample_certificate) -> None:
    assert sample_certificate.pem.startswith("-----BEGIN CERTIFICATE-----")
    assert sample_certificate.pem.strip().endswith("-----END CERTIFICATE-----")


def test_build_ssl_context_pins_and_skips_hostname_check(sample_certificate) -> None:
    ctx = build_ssl_context(sample_certificate.pem)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is False


@contextlib.asynccontextmanager
async def _tls_server(cert_pem: str, key_pem: str):
    """A minimal TLS server on an ephemeral port, torn down on exit."""
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    with (
        tempfile.NamedTemporaryFile("w", suffix=".pem") as certfile,
        tempfile.NamedTemporaryFile("w", suffix=".pem") as keyfile,
    ):
        certfile.write(cert_pem)
        certfile.flush()
        keyfile.write(key_pem)
        keyfile.flush()
        server_ctx.load_cert_chain(certfile.name, keyfile.name)

        async def handle(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=server_ctx)
        async with server:
            yield server.sockets[0].getsockname()[1]


async def test_fetch_and_pin_round_trip(cert_factory, socket_enabled) -> None:
    """fetch → pin → connect succeeds against the real server; a client
    pinned to a *different* certificate is rejected by the same server.

    Needs real loopback sockets, which pytest-homeassistant-custom-component
    blocks by default (`socket_enabled` opts this one test back in).
    """
    real = cert_factory()
    impostor = cert_factory()

    async with _tls_server(real.cert_pem, real.key_pem) as port:
        # 1. Trust-on-first-use fetch — no verification has happened yet.
        fetched_der = await fetch_server_certificate("127.0.0.1", port)
        assert fetched_der == real.der

        # 2. Pinning the certificate that was actually fetched connects fine.
        good_ctx = build_ssl_context(pem_from_der(fetched_der))
        _reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=good_ctx
        )
        writer.close()
        await writer.wait_closed()

        # 3. Pinning a different certificate rejects the same server.
        bad_ctx = build_ssl_context(impostor.cert_pem)
        with pytest.raises(ssl.SSLCertVerificationError):
            await asyncio.open_connection("127.0.0.1", port, ssl=bad_ctx)
