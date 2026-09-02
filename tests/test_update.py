"""Tests for the App update entity."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras, default_update_status

BASE_STATUS = {
    "collectedAt": "2026-08-30T12:00:00.000Z",
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
}


def _bundle(update_status: dict) -> dict:
    return {
        **bundle_extras(),
        "status": BASE_STATUS,
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [],
        "update-status": update_status,
    }


async def _setup_entry(hass: HomeAssistant, sample_certificate, mock_client_cls, bundle):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={
            "host": "192.168.1.50",
            "port": 8734,
            "token": "a" * 64,
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )
    entry.add_to_hass(hass)

    async def _open_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    mock_client_cls.return_value.async_stream_updates = _open_stream
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant) -> str:
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "update", DOMAIN, "192.168.1.50:8734_app_update"
    )
    assert entity_id is not None
    return entity_id


async def test_reflects_available_update(
    hass: HomeAssistant, sample_certificate
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        state = hass.states.get(_entity_id(hass))

    assert state.state == "on"
    assert state.attributes["installed_version"] == "0.3.0"
    assert state.attributes["latest_version"] == "0.4.0"
    assert (
        state.attributes["release_url"]
        == "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0"
    )
    assert state.attributes["in_progress"] is False


async def test_no_check_run_yet_is_unknown(
    hass: HomeAssistant, sample_certificate
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(
            hass, sample_certificate, mock_client_cls, _bundle(default_update_status())
        )
        state = hass.states.get(_entity_id(hass))

    assert state.state == "unknown"


async def test_up_to_date_is_off(hass: HomeAssistant, sample_certificate) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.3.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": False,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        state = hass.states.get(_entity_id(hass))

    assert state.state == "off"


async def test_install_starts_download_and_refreshes_status(
    hass: HomeAssistant, sample_certificate
) -> None:
    """update.install has to actually start the download (the only write
    the real API offers) and then re-fetch /api/update-status itself,
    since a write to update-status.json doesn't trigger a stream push the
    way a real collection pass does."""
    initial = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://example.com/release",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    refreshed = {**initial, "downloading": True}

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_download_update = AsyncMock()
        mock_client_cls.return_value.async_get_update_status = AsyncMock(
            return_value=refreshed
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(initial))
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )

        mock_client_cls.return_value.async_download_update.assert_called_once()
        mock_client_cls.return_value.async_get_update_status.assert_called_once()
        state = hass.states.get(entity_id)
        assert state.attributes["in_progress"] is True


async def test_install_surfaces_connection_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_download_update = AsyncMock(
            side_effect=ClassDashConnectionError("refused")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )


async def test_install_surfaces_auth_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_download_update = AsyncMock(
            side_effect=ClassDashAuthError("bad token")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )


async def test_install_refresh_failure_does_not_raise(
    hass: HomeAssistant, sample_certificate
) -> None:
    """The download itself already started — a failed follow-up refetch
    shouldn't turn that into a service-call error, the entity just stays
    on whatever it last had until the next real push."""
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_download_update = AsyncMock()
        mock_client_cls.return_value.async_get_update_status = AsyncMock(
            side_effect=ClassDashConnectionError("dropped")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )
        mock_client_cls.return_value.async_download_update.assert_called_once()
