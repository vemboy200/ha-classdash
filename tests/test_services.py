"""Tests for the hide/unhide/mute/unmute and virtual-reminder service
actions."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    ClassDashValidationError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.devices import class_device_info, main_device_info
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


def _class_device_id(hass: HomeAssistant, entry: MockConfigEntry, class_name: str) -> str:
    """Registers (or looks up) a class sub-device the way sensor.py/
    calendar.py normally would once a class shows up in real data —
    tests exercising the `class` field's device selector need a real
    device id to select, same as the actual UI would offer."""
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **class_device_info(entry, class_name)
    )
    return device.id


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

    for service in (
        "hide",
        "unhide",
        "mute",
        "unmute",
        "create_virtual_reminder",
        "edit_virtual_reminder",
    ):
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


CREATED_ENTRY = {
    "id": "v-abc123",
    "title": "Bring signed permission slip",
    "class": "Physics",
    "due": "2026-09-10T14:30:00+00:00",
    "createdAt": "2026-09-02T00:00:00.000Z",
    "done": False,
    "doneAt": None,
    "hidden": False,
}


async def test_create_virtual_reminder_calls_client_and_refreshes(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock(
        return_value={"ok": True, "entry": CREATED_ENTRY}
    )
    client.async_get_virtual = AsyncMock(return_value=[CREATED_ENTRY])
    await _setup(hass, entry, client)
    physics_device_id = _class_device_id(hass, entry, "Physics")

    response = await hass.services.async_call(
        DOMAIN,
        "create_virtual_reminder",
        {
            "title": "Bring signed permission slip",
            "class": physics_device_id,
            "due": "2026-09-10T14:30:00+00:00",
        },
        blocking=True,
        return_response=True,
    )

    client.async_create_virtual_reminder.assert_called_once_with(
        title="Bring signed permission slip",
        class_name="Physics",
        due="2026-09-10T14:30:00+00:00",
    )
    # update-status.json's own gap all over again: a write to
    # virtual-assignments.json doesn't trigger its own /api/stream
    # broadcast either, so this has to refetch directly.
    client.async_get_virtual.assert_called_once()
    assert response == CREATED_ENTRY


async def test_create_virtual_reminder_without_class_or_due(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock(
        return_value={"ok": True, "entry": CREATED_ENTRY}
    )
    client.async_get_virtual = AsyncMock(return_value=[])
    await _setup(hass, entry, client)

    await hass.services.async_call(
        DOMAIN,
        "create_virtual_reminder",
        {"title": "General reminder"},
        blocking=True,
    )

    client.async_create_virtual_reminder.assert_called_once_with(
        title="General reminder", class_name=None, due=None
    )


async def test_edit_virtual_reminder_calls_client_and_refreshes(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    edited = {**CREATED_ENTRY, "title": "Bring signed AND initialed slip"}
    client.async_edit_virtual_reminder = AsyncMock(
        return_value={"ok": True, "entry": edited}
    )
    client.async_get_virtual = AsyncMock(return_value=[edited])
    await _setup(hass, entry, client)
    physics_device_id = _class_device_id(hass, entry, "Physics")

    response = await hass.services.async_call(
        DOMAIN,
        "edit_virtual_reminder",
        {
            "id": "v-abc123",
            "title": "Bring signed AND initialed slip",
            "class": physics_device_id,
            "due": "2026-09-10T14:30:00+00:00",
        },
        blocking=True,
        return_response=True,
    )

    client.async_edit_virtual_reminder.assert_called_once_with(
        item_id="v-abc123",
        title="Bring signed AND initialed slip",
        class_name="Physics",
        due="2026-09-10T14:30:00+00:00",
    )
    client.async_get_virtual.assert_called_once()
    assert response == edited


async def test_create_virtual_reminder_rejects_unknown_class_device(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock()
    await _setup(hass, entry, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "create_virtual_reminder",
            {"title": "Whatever", "class": "nonexistent-device-id"},
            blocking=True,
        )
    client.async_create_virtual_reminder.assert_not_called()


async def test_create_virtual_reminder_rejects_main_device_as_class(
    hass: HomeAssistant, sample_certificate
) -> None:
    """The device selector is scoped to model "Class" so the main
    "ClassDash" device shouldn't be pickable, but Developer Tools/YAML
    can still pass any device id directly — the service itself has to
    reject it too, not just rely on the UI filter."""
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock()
    await _setup(hass, entry, client)
    main_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **main_device_info(entry)
    )

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "create_virtual_reminder",
            {"title": "Whatever", "class": main_device.id},
            blocking=True,
        )
    client.async_create_virtual_reminder.assert_not_called()


async def test_create_virtual_reminder_rejects_class_device_from_other_entry(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry_a = _entry("192.168.1.50:8734", sample_certificate.pem)
    client_a = AsyncMock()
    client_a.async_stream_updates = _open_stream
    client_a.async_create_virtual_reminder = AsyncMock()
    await _setup(hass, entry_a, client_a)

    entry_b = _entry("192.168.1.51:8734", sample_certificate.pem)
    client_b = AsyncMock()
    client_b.async_stream_updates = _open_stream
    await _setup(hass, entry_b, client_b)
    other_entrys_device_id = _class_device_id(hass, entry_b, "Physics")

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "create_virtual_reminder",
            {
                "title": "Whatever",
                "class": other_entrys_device_id,
                "config_entry_id": entry_a.entry_id,
            },
            blocking=True,
        )
    client_a.async_create_virtual_reminder.assert_not_called()


async def test_create_virtual_reminder_surfaces_validation_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock(
        side_effect=ClassDashValidationError("title is required")
    )
    await _setup(hass, entry, client)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "create_virtual_reminder",
            {"title": "Whatever"},
            blocking=True,
        )
    client.async_get_virtual.assert_not_called()


async def test_edit_virtual_reminder_surfaces_connection_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_edit_virtual_reminder = AsyncMock(
        side_effect=ClassDashConnectionError("refused")
    )
    await _setup(hass, entry, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "edit_virtual_reminder",
            {"id": "v-abc123", "title": "Whatever"},
            blocking=True,
        )


async def test_create_virtual_reminder_surfaces_auth_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock(
        side_effect=ClassDashAuthError("bad token")
    )
    await _setup(hass, entry, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "create_virtual_reminder",
            {"title": "Whatever"},
            blocking=True,
        )


async def test_refresh_after_write_swallows_its_own_failure(
    hass: HomeAssistant, sample_certificate
) -> None:
    """The write itself already succeeded — a failed follow-up
    /api/virtual refetch shouldn't turn that into a service-call error,
    same reasoning as update.py's own best-effort refresh."""
    entry = _entry("192.168.1.50:8734", sample_certificate.pem)
    client = AsyncMock()
    client.async_stream_updates = _open_stream
    client.async_create_virtual_reminder = AsyncMock(
        return_value={"ok": True, "entry": CREATED_ENTRY}
    )
    client.async_get_virtual = AsyncMock(
        side_effect=ClassDashConnectionError("dropped")
    )
    await _setup(hass, entry, client)

    response = await hass.services.async_call(
        DOMAIN,
        "create_virtual_reminder",
        {"title": "Bring signed permission slip"},
        blocking=True,
        return_response=True,
    )
    assert response == CREATED_ENTRY
