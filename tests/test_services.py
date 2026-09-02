"""Tests for the hide/unhide/mute/unmute service actions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

FAKE_BUNDLE = {
    **bundle_extras(),
    "status": {
        "collectedAt": "2026-09-01T00:00:00.000Z",
        "minutesAgo": 1,
        "classes": 0,
        "total": 0,
        "dueSoon": 0,
        "overdue": 0,
        "ahead": 0,
        "done": 0,
        "announcements": 0,
        "removed": 0,
        "language": "en",
    },
    "due-soon": [],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [],
}


def _entry(unique_id: str, cert_pem: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=unique_id,
        data={
            "host": unique_id.split(":")[0],
            "port": int(unique_id.split(":")[1]),
            "token": "a" * 64,
            CONF_CERT_PEM: cert_pem,
        },
    )


async def _open_stream():
    yield StreamEvent("update", FAKE_BUNDLE)
    await asyncio.Event().wait()


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, client_mock) -> None:
    entry.add_to_hass(hass)
    with patch(
        "custom_components.classdash.ClassDashClient",
        autospec=True,
        return_value=client_mock,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def _setup_component_only(hass: HomeAssistant) -> bool:
    """Registers services without any config entry — the same state HA
    is in right after startup, before the first ClassDash entry loads."""
    from homeassistant.setup import async_setup_component

    return await async_setup_component(hass, DOMAIN, {})


async def test_services_are_registered_after_setup(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    await _setup(hass, entry, client)

    for service in ("hide", "unhide", "mute", "unmute"):
        assert hass.services.has_service(DOMAIN, service)


async def test_hide_service_calls_client_with_single_loaded_entry(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    await _setup(hass, entry, client)

    await hass.services.async_call(
        DOMAIN, "hide", {"id": "abc123"}, blocking=True
    )
    client.async_hide.assert_called_once_with("abc123")


async def test_mute_and_unmute_call_the_right_client_methods(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    await _setup(hass, entry, client)

    await hass.services.async_call(DOMAIN, "mute", {"id": "x1"}, blocking=True)
    client.async_mute.assert_called_once_with("x1")

    await hass.services.async_call(DOMAIN, "unmute", {"id": "x1"}, blocking=True)
    client.async_unmute.assert_called_once_with("x1")

    await hass.services.async_call(DOMAIN, "unhide", {"id": "x1"}, blocking=True)
    client.async_unhide.assert_called_once_with("x1")


async def test_service_without_config_entry_id_fails_when_none_loaded(
    hass: HomeAssistant,
) -> None:
    assert await _setup_component_only(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "hide", {"id": "x1"}, blocking=True)


async def test_service_fails_when_multiple_entries_loaded_without_id(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry_a = _entry("192.168.1.50:8734", sample_certificate.pem)
    client_a = AsyncMock()
    client_a.async_stream_updates = _open_stream
    await _setup(hass, entry_a, client_a)

    entry_b = _entry("192.168.1.51:8734", sample_certificate.pem)
    client_b = AsyncMock()
    client_b.async_stream_updates = _open_stream
    await _setup(hass, entry_b, client_b)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "hide", {"id": "x1"}, blocking=True)


async def test_service_with_explicit_config_entry_id_targets_right_entry(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry_a = _entry("192.168.1.50:8734", sample_certificate.pem)
    client_a = AsyncMock()
    client_a.async_stream_updates = _open_stream
    await _setup(hass, entry_a, client_a)

    entry_b = _entry("192.168.1.51:8734", sample_certificate.pem)
    client_b = AsyncMock()
    client_b.async_stream_updates = _open_stream
    await _setup(hass, entry_b, client_b)

    await hass.services.async_call(
        DOMAIN,
        "hide",
        {"id": "x1", "config_entry_id": entry_b.entry_id},
        blocking=True,
    )
    client_b.async_hide.assert_called_once_with("x1")
    client_a.async_hide.assert_not_called()


async def test_service_rejects_unknown_config_entry_id(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    await _setup(hass, entry, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "hide", {"id": "x1", "config_entry_id": "nonexistent"}, blocking=True
        )


async def test_service_surfaces_connection_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_hide = AsyncMock(side_effect=ClassDashConnectionError("refused"))
    await _setup(hass, entry, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "hide", {"id": "x1"}, blocking=True)


async def test_service_surfaces_auth_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_mute = AsyncMock(side_effect=ClassDashAuthError("bad token"))
    await _setup(hass, entry, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "mute", {"id": "x1"}, blocking=True)
