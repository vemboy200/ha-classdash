"""Fixtures for ClassDash tests."""

from __future__ import annotations

import datetime
from collections.abc import Callable, Generator
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.classdash.api import fingerprint_from_der, pem_from_der

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations for every test."""


def unknown_check_status() -> dict[str, dict[str, None | str]]:
    """The three-platform check-status shape, all "unknown" — a
    reasonable default for tests that don't care about pipeline health
    specifically. Matches checkStatus()'s own UNKNOWN constant in
    25-check-status.js."""
    unknown = {"status": "unknown", "at": None, "detail": None}
    return {"classroom": dict(unknown), "canvas": dict(unknown), "edpuzzle": dict(unknown)}


def bundle_extras() -> dict:
    """The newer top-level snapshot keys (done/check-status/virtual) that
    every bundle fixture needs now that ClassDashData requires them —
    spread into a test's own bundle dict so each one doesn't have to
    repeat this shape by hand."""
    return {"done": [], "check-status": unknown_check_status(), "virtual": []}


@dataclass
class GeneratedCertificate:
    """A real, throwaway self-signed cert — same shape ClassDash's own
    23-api-security.js generates (RSA 2048, self-signed, CN=classdash-local).
    """

    der: bytes
    cert_pem: str
    key_pem: str


def _generate_certificate() -> GeneratedCertificate:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "classdash-local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return GeneratedCertificate(
        der=cert.public_bytes(serialization.Encoding.DER),
        cert_pem=cert.public_bytes(serialization.Encoding.PEM).decode(),
        key_pem=key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ).decode(),
    )


@pytest.fixture
def cert_factory() -> Callable[[], GeneratedCertificate]:
    """Generates a fresh self-signed cert+key pair on each call."""
    return _generate_certificate


@dataclass
class SampleCertificate:
    """A generated certificate plus its derived pinning values."""

    der: bytes
    pem: str
    fingerprint: str


@pytest.fixture
def sample_certificate() -> SampleCertificate:
    """A single generated certificate, for tests that don't need the server side."""
    generated = _generate_certificate()
    return SampleCertificate(
        der=generated.der,
        pem=pem_from_der(generated.der),
        fingerprint=fingerprint_from_der(generated.der),
    )


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Stub out the integration's real setup/unload for flow-only tests."""
    with (
        patch(
            "custom_components.classdash.async_setup_entry", return_value=True
        ) as mock_setup,
        patch("custom_components.classdash.async_unload_entry", return_value=True),
    ):
        yield mock_setup
