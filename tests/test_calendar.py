"""Tests for the per-class calendar entity's event logic.

Constructs ClassDashClassCalendar directly against a lightweight fake
coordinator/entry rather than going through full config entry setup —
this is pure date-range logic, not anything HA's entity lifecycle needs
to be involved in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from custom_components.classdash.calendar import ClassDashClassCalendar
from custom_components.classdash.coordinator import ClassDashData


class _FakeEntry:
    unique_id = "192.168.1.50:8734"


class _FakeCoordinator:
    def __init__(self, data: ClassDashData) -> None:
        self.data = data


def _assignment(title: str, due_iso: str, class_name: str = "Physics", item_id: str = "a1"):
    return {
        "id": item_id,
        "title": title,
        "class": class_name,
        "due": due_iso,
        "link": f"https://example.com/{item_id}",
    }


def _calendar(data: ClassDashData) -> ClassDashClassCalendar:
    return ClassDashClassCalendar(_FakeCoordinator(data), _FakeEntry(), "Physics")


def test_event_is_the_soonest_not_yet_ended() -> None:
    data = ClassDashData(
        status={},
        due_soon=[_assignment("Lab report", "2026-09-05T23:59:00+00:00", item_id="soon")],
        ahead=[_assignment("Final project", "2026-10-01T23:59:00+00:00", item_id="ahead")],
        overdue=[_assignment("Old worksheet", "2026-08-01T23:59:00+00:00", item_id="overdue")],
        announcements=[],
        classes=[],
    )
    event = _calendar(data).event
    assert event is not None
    assert event.uid == "soon"
    assert event.summary == "Lab report"


def test_event_is_none_when_everything_is_over() -> None:
    data = ClassDashData(
        status={},
        due_soon=[],
        ahead=[],
        overdue=[_assignment("Old worksheet", "2020-01-01T23:59:00+00:00")],
        announcements=[],
        classes=[],
    )
    assert _calendar(data).event is None


def test_event_ignores_other_classes() -> None:
    data = ClassDashData(
        status={},
        due_soon=[_assignment("Not physics", "2026-09-05T23:59:00+00:00", class_name="Biology")],
        ahead=[],
        overdue=[],
        announcements=[],
        classes=[],
    )
    assert _calendar(data).event is None


async def test_async_get_events_includes_overdue_in_range() -> None:
    """Overdue items don't show up as `event`, but should still appear
    when a caller (e.g. the calendar UI, viewing a past week) asks for
    events in a range that covers them — this is the difference between
    "due-soon + ahead only" and "everything with a due date"."""
    data = ClassDashData(
        status={},
        due_soon=[_assignment("Upcoming", "2026-09-05T23:59:00+00:00", item_id="upcoming")],
        ahead=[],
        overdue=[_assignment("Late", "2026-08-01T23:59:00+00:00", item_id="late")],
        announcements=[],
        classes=[],
    )
    events = await _calendar(data).async_get_events(
        hass=None,
        start_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    assert {e.uid for e in events} == {"upcoming", "late"}


async def test_async_get_events_respects_the_range() -> None:
    data = ClassDashData(
        status={},
        due_soon=[_assignment("In range", "2026-09-05T23:59:00+00:00", item_id="in")],
        ahead=[_assignment("Out of range", "2027-01-01T23:59:00+00:00", item_id="out")],
        overdue=[],
        announcements=[],
        classes=[],
    )
    events = await _calendar(data).async_get_events(
        hass=None,
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert {e.uid for e in events} == {"in"}
