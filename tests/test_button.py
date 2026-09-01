"""Tests for the Reload/Check now buttons."""

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

from .conftest import bundle_extras

FAKE_BUNDLE = {
    **bundle_extras(),
    "status": {
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
    },
    "due-soon": [],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [],
}


async def _open_stream():
    yield StreamEvent("update", FAKE_BUNDLE)
    await asyncio.Event().wait()


async def _setup_entry(hass: HomeAssistant, sample_certificate, mock_client_cls):
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
    mock_client_cls.return_value.async_stream_updates = _open_stream
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_reload_button_calls_client_and_creates_no_stale_state(
    hass: HomeAssistant, sample_certificate
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_reload = AsyncMock()
        await _setup_entry(hass, sample_certificate, mock_client_cls)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "button", DOMAIN, "192.168.1.50:8734_reload"
        )
        assert entity_id is not None

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        mock_client_cls.return_value.async_reload.assert_called_once()


async def test_check_button_calls_client(
    hass: HomeAssistant, sample_certificate
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_check = AsyncMock()
        await _setup_entry(hass, sample_certificate, mock_client_cls)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "button", DOMAIN, "192.168.1.50:8734_check"
        )
        assert entity_id is not None

        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        mock_client_cls.return_value.async_check.assert_called_once()


async def test_reload_button_surfaces_connection_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_reload = AsyncMock(
            side_effect=ClassDashConnectionError("refused")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "button", DOMAIN, "192.168.1.50:8734_reload"
        )

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )


async def test_check_button_surfaces_auth_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_check = AsyncMock(
            side_effect=ClassDashAuthError("bad token")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls)

        ent_reg = er.async_get(hass)
        entity_id = ent_reg.async_get_entity_id(
            "button", DOMAIN, "192.168.1.50:8734_check"
        )

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "button", "press", {"entity_id": entity_id}, blocking=True
            )
