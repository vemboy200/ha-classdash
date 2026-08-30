"""End-to-end tests for per-class devices: created dynamically from live
data, linked to the main device, and holding both the count sensors and
the calendar entity."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.devices import class_unique_id
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _assignment(class_name: str, title: str, due: str, item_id: str) -> dict:
    return {"id": item_id, "title": title, "class": class_name, "due": due, "link": None}


def _status(**counts) -> dict:
    return {
        "collectedAt": "2026-08-30T12:00:00.000Z",
        "minutesAgo": 1,
        "classes": 2,
        "total": sum(counts.values()),
        "dueSoon": counts.get("due_soon", 0),
        "overdue": counts.get("overdue", 0),
        "ahead": counts.get("ahead", 0),
        "announcements": counts.get("announcements", 0),
        "removed": 0,
        "language": "en",
    }


def _bundle(due_soon=(), ahead=(), overdue=(), announcements=()) -> dict:
    return {
        "status": _status(
            due_soon=len(due_soon),
            ahead=len(ahead),
            overdue=len(overdue),
            announcements=len(announcements),
        ),
        "due-soon": list(due_soon),
        "ahead": list(ahead),
        "overdue": list(overdue),
        "announcements": list(announcements),
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


async def test_class_devices_created_with_correct_entities_and_linkage(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-01T23:59:00+00:00", "p1")],
        overdue=[
            _assignment(
                "AP Chem, Period 2!", "Worksheet", "2026-08-20T23:59:00+00:00", "c1"
            )
        ],
    )

    async def fake_stream():
        yield bundle
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    main_device = dev_reg.async_get_device({(DOMAIN, entry.unique_id)})
    assert main_device is not None

    physics_device = dev_reg.async_get_device(
        {(DOMAIN, class_unique_id(entry, "Physics"))}
    )
    assert physics_device is not None
    assert physics_device.via_device_id == main_device.id
    assert physics_device.name == "Physics"

    chem_device = dev_reg.async_get_device(
        {(DOMAIN, class_unique_id(entry, "AP Chem, Period 2!"))}
    )
    assert chem_device is not None
    assert chem_device.via_device_id == main_device.id

    def _state_for(unique_id: str, domain: str = "sensor"):
        entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id is not None, f"no entity registered for {unique_id}"
        return hass.states.get(entity_id)

    physics_due_soon = _state_for(f"{class_unique_id(entry, 'Physics')}_due_soon")
    assert physics_due_soon.state == "1"
    physics_overdue = _state_for(f"{class_unique_id(entry, 'Physics')}_overdue")
    assert physics_overdue.state == "0"

    chem_overdue = _state_for(f"{class_unique_id(entry, 'AP Chem, Period 2!')}_overdue")
    assert chem_overdue.state == "1"

    physics_calendar = _state_for(
        f"{class_unique_id(entry, 'Physics')}_calendar", domain="calendar"
    )
    assert physics_calendar is not None
    assert physics_calendar.attributes["message"] == "Lab report"


async def test_a_class_appearing_later_gets_its_own_entities(
    hass: HomeAssistant, sample_certificate
) -> None:
    """No entry reload needed — a class showing up for the first time in a
    later push should get entities immediately."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    first = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-01T23:59:00+00:00", "p1")]
    )
    second = _bundle(
        due_soon=[
            _assignment("Physics", "Lab report", "2026-09-01T23:59:00+00:00", "p1"),
            _assignment("Biology", "Reading", "2026-09-02T23:59:00+00:00", "b1"),
        ]
    )
    second_pushed = asyncio.Event()

    async def fake_stream():
        yield first
        yield second
        second_pushed.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(second_pushed.wait(), timeout=2)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{class_unique_id(entry, 'Biology')}_due_soon"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "1"
