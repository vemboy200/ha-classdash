"""End-to-end tests for the check-status sensors, the Done sensor
(main + per-class), and virtual reminders appearing on class calendars —
the three pieces of ClassDash's API added after per-class devices were
already built."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.classdash.api import StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.devices import class_unique_id
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras


def _assignment(class_name: str, title: str, due: str, item_id: str, tags=()) -> dict:
    return {
        "id": item_id,
        "title": title,
        "class": class_name,
        "due": due,
        "link": None,
        "tags": list(tags),
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


async def _setup_with_bundle(hass, sample_certificate, bundle) -> MockConfigEntry:
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    async def fake_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_check_status_sensors_reflect_platform_health(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = {
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
        "check-status": {
            "classroom": {"status": "ok", "at": "2026-09-01T00:00:00.000Z", "detail": None},
            "canvas": {
                "status": "problem",
                "at": "2026-08-31T23:00:00.000Z",
                "detail": "timed out",
            },
            "edpuzzle": {"status": "unknown", "at": None, "detail": None},
        },
    }
    entry = await _setup_with_bundle(hass, sample_certificate, bundle)
    ent_reg = er.async_get(hass)

    def _state_for(key: str):
        entity_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id}_{key}")
        assert entity_id is not None, f"no entity for {key}"
        return hass.states.get(entity_id)

    classroom = _state_for("check_status_classroom")
    assert classroom.state == "ok"
    assert classroom.attributes["at"] == "2026-09-01T00:00:00.000Z"
    assert classroom.attributes["detail"] is None

    canvas = _state_for("check_status_canvas")
    assert canvas.state == "problem"
    assert canvas.attributes["detail"] == "timed out"

    edpuzzle = _state_for("check_status_edpuzzle")
    assert edpuzzle.state == "unknown"


async def test_done_sensor_main_and_per_class(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = {
        **bundle_extras(),
        "status": {
            "collectedAt": "2026-09-01T00:00:00.000Z",
            "minutesAgo": 1,
            "classes": 1,
            "total": 1,
            "dueSoon": 0,
            "overdue": 0,
            "ahead": 0,
            "done": 1,
            "announcements": 0,
            "removed": 0,
            "language": "en",
        },
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "done": [
            _assignment(
                "Physics", "Finished lab", "2026-08-20T00:00:00+00:00", "d1", tags=["done"]
            )
        ],
        "announcements": [],
        "classes": [],
    }
    entry = await _setup_with_bundle(hass, sample_certificate, bundle)
    ent_reg = er.async_get(hass)

    main_done_id = ent_reg.async_get_entity_id("sensor", DOMAIN, f"{entry.unique_id}_done")
    assert hass.states.get(main_done_id).state == "1"

    class_done_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{class_unique_id(entry, 'Physics')}_done"
    )
    assert class_done_id is not None
    class_done_state = hass.states.get(class_done_id)
    assert class_done_state.state == "1"
    assert class_done_state.attributes["assignments"][0]["title"] == "Finished lab"


async def test_virtual_reminder_appears_on_class_calendar(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = {
        **bundle_extras(),
        "status": {
            "collectedAt": "2026-09-01T00:00:00.000Z",
            "minutesAgo": 1,
            "classes": 1,
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
        "virtual": [
            _assignment("Physics", "Study for the final", "2026-09-15T00:00:00+00:00", "v1")
        ],
    }
    entry = await _setup_with_bundle(hass, sample_certificate, bundle)
    ent_reg = er.async_get(hass)

    calendar_id = ent_reg.async_get_entity_id(
        "calendar", DOMAIN, f"{class_unique_id(entry, 'Physics')}_calendar"
    )
    assert calendar_id is not None
    assert hass.states.get(calendar_id).attributes["message"] == "Study for the final"


async def test_done_and_hidden_virtual_reminders_excluded_from_calendar(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = {
        **bundle_extras(),
        "status": {
            "collectedAt": "2026-09-01T00:00:00.000Z",
            "minutesAgo": 1,
            "classes": 1,
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
        "virtual": [
            _assignment(
                "Physics",
                "Already done",
                "2026-09-10T00:00:00+00:00",
                "v1",
                tags=["done"],
            ),
            _assignment(
                "Physics",
                "Dismissed",
                "2026-09-11T00:00:00+00:00",
                "v2",
                tags=["hidden"],
            ),
            _assignment(
                "Physics", "Still relevant", "2026-09-12T00:00:00+00:00", "v3"
            ),
        ],
    }
    entry = await _setup_with_bundle(hass, sample_certificate, bundle)
    ent_reg = er.async_get(hass)

    calendar_id = ent_reg.async_get_entity_id(
        "calendar", DOMAIN, f"{class_unique_id(entry, 'Physics')}_calendar"
    )
    assert hass.states.get(calendar_id).attributes["message"] == "Still relevant"


async def test_virtual_reminder_with_no_class_gets_no_calendar(
    hass: HomeAssistant, sample_certificate
) -> None:
    """A reminder that isn't assigned to any class shouldn't create a
    device/calendar of its own — there's nowhere for it to go."""
    bundle = {
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
        "virtual": [_assignment(None, "General reminder", "2026-09-10T00:00:00+00:00", "v1")],
    }
    await _setup_with_bundle(hass, sample_certificate, bundle)
    ent_reg = er.async_get(hass)
    # No class name means no class_unique_id to look up — just confirm
    # nothing unexpected got created by counting calendar entities.
    calendars = [
        e for e in ent_reg.entities.values() if e.domain == "calendar" and e.platform == DOMAIN
    ]
    assert calendars == []
