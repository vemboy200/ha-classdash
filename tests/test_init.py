"""Tests for setting up and unloading a ClassDash config entry."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.classdash.api import ClassDashAuthError, StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

FAKE_STATUS = {
    "collectedAt": "2026-08-30T12:00:00.000Z",
    "minutesAgo": 3,
    "classes": 6,
    "total": 12,
    "dueSoon": 2,
    "overdue": 1,
    "ahead": 9,
    "announcements": 4,
    "removed": 0,
    "language": "en",
}
FAKE_BUNDLE = {
    "status": FAKE_STATUS,
    "due-soon": [],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [],
}


def _make_entry(sample_certificate) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={
            "host": "192.168.1.50",
            "port": 8734,
            "token": "a" * 64,
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )


async def _open_stream_stub():
    """A stream that pushes one snapshot and then stays connected."""
    yield StreamEvent("update", FAKE_BUNDLE)
    await asyncio.Event().wait()


async def test_setup_and_unload_entry(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = _open_stream_stub

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED

        due_soon = hass.states.get("sensor.classdash_due_soon")
        assert due_soon is not None
        assert due_soon.state == "2"

        last_collected = hass.states.get("sensor.classdash_last_collected")
        assert last_collected is not None
        assert last_collected.attributes["minutes_ago"] == 3

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_triggers_reauth_on_bad_token(
    hass: HomeAssistant, sample_certificate
) -> None:
    """A bad/expired token on the *very first* connection should fail setup
    in a way Home Assistant recognizes as needing reauth."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    async def failing_stream():
        raise ClassDashAuthError("bad token")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = failing_stream

        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(flow["context"].get("source") == "reauth" for flow in flows)
