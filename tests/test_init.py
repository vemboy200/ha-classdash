"""Tests for setting up and unloading a ClassDash config entry."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

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


async def test_setup_and_unload_entry(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        client = mock_client_cls.return_value
        client.async_get_status = AsyncMock(return_value=FAKE_STATUS)
        client.async_get_due_soon = AsyncMock(return_value=[])
        client.async_get_ahead = AsyncMock(return_value=[])
        client.async_get_overdue = AsyncMock(return_value=[])
        client.async_get_announcements = AsyncMock(return_value=[])

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
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    from custom_components.classdash.api import ClassDashAuthError

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_get_status = AsyncMock(
            side_effect=ClassDashAuthError("bad token")
        )
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
