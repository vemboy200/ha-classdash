"""Tests for the School today, Schedule today and School calendar entities."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.classdash.api import StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

STATUS = {
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
    "collecting": False,
}


def _day(offset: int) -> str:
    # Relative to today: the calendar's event and range filtering read the
    # real clock, so fixed dates would stop working once they pass.
    return (dt_util.now().date() + timedelta(days=offset)).isoformat()


def _bundle() -> dict:
    return {
        **bundle_extras(),
        "status": {**STATUS, "schoolToday": True, "scheduleToday": "Even"},
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [],
        "calendar": {
            "available": True,
            "today": {
                "date": _day(0),
                "kind": "minimumDay",
                "label": "Early release",
                "events": ["Picture day"],
                "schoolDay": True,
            },
            "nextSchoolDay": _day(1),
            "upcoming": [
                {"from": _day(0), "to": _day(0), "kind": "minimumDay", "label": "Early release"},
                {"from": _day(3), "to": _day(4), "kind": "noSchool", "label": ""},
            ],
            "events": [{"from": _day(2), "to": _day(2), "summary": "Back to school night"}],
        },
        "schedule": {
            "type": "oddEven",
            "today": {
                "date": _day(0),
                "schoolDay": True,
                "day": "B",
                "flipped": False,
                "label": "Even",
                "classes": [
                    {"class": "Chemistry", "period": 4},
                    {"class": "Physics", "period": 2},
                ],
            },
            "nextSchoolDay": {
                "date": _day(1),
                "schoolDay": True,
                "day": "A",
                "flipped": False,
                "label": "Odd",
                "classes": [{"class": "English", "period": 1}],
            },
            "headsUps": [],
        },
    }


async def _setup(hass: HomeAssistant, sample_certificate, bundle: dict) -> MockConfigEntry:
    async def stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

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
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        async with asyncio.timeout(2):
            while hass.states.get("binary_sensor.classdash_school_today").state == "unavailable":
                await asyncio.sleep(0)
    return entry


async def test_school_today_and_schedule_today(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = await _setup(hass, sample_certificate, _bundle())

    school = hass.states.get("binary_sensor.classdash_school_today")
    assert school.state == "on"
    assert school.attributes["day_kind"] == "minimum_day"
    assert school.attributes["label"] == "Early release"
    assert school.attributes["events"] == ["Picture day"]
    assert school.attributes["next_school_day"] == _day(1)

    schedule = hass.states.get("sensor.classdash_schedule_today")
    assert schedule.state == "Even"
    assert schedule.attributes["schedule_type"] == "oddEven"
    # Sorted by period, not in the order ClassDash happened to list them.
    assert schedule.attributes["classes"] == [
        {"period": 2, "class": "Physics"},
        {"period": 4, "class": "Chemistry"},
    ]
    assert schedule.attributes["next_school_day"] == {
        "date": _day(1),
        "label": "Odd",
        "classes": [{"period": 1, "class": "English"}],
    }

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_without_a_school_calendar_school_today_is_unknown(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = {
        **bundle_extras(),
        "status": {**STATUS, "schoolToday": None, "scheduleToday": None},
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [],
    }
    entry = await _setup(hass, sample_certificate, bundle)

    assert hass.states.get("binary_sensor.classdash_school_today").state == "unknown"
    assert hass.states.get("sensor.classdash_schedule_today").state == "unknown"
    assert hass.states.get("calendar.classdash_school_calendar").state == "off"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_school_calendar_events(hass: HomeAssistant, sample_certificate) -> None:
    entry = await _setup(hass, sample_certificate, _bundle())

    # Today's minimum day is an all-day event that's on right now.
    calendar = hass.states.get("calendar.classdash_school_calendar")
    assert calendar.state == "on"
    assert calendar.attributes["message"] == "Early release"

    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.classdash_school_calendar",
            "start_date_time": dt_util.now() - timedelta(days=1),
            "end_date_time": dt_util.now() + timedelta(days=14),
        },
        blocking=True,
        return_response=True,
    )
    events = result["calendar.classdash_school_calendar"]["events"]
    assert [(e["summary"], e["start"], e["end"]) for e in events] == [
        ("Early release", _day(0), _day(1)),
        ("Back to school night", _day(2), _day(3)),
        # No label of its own, so it's named by its kind. Two days long:
        # ClassDash's `to` is the last day, an all-day event ends the next.
        ("No school", _day(3), _day(5)),
    ]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
