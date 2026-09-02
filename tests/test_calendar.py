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


def _assignment(
    title: str, due_iso: str, class_name: str = "Physics", item_id: str = "a1", tags=()
):
    return {
        "id": item_id,
        "title": title,
        "class": class_name,
        "due": due_iso,
        "link": f"https://example.com/{item_id}",
        "tags": list(tags),
    }


def _calendar(data: ClassDashData) -> ClassDashClassCalendar:
    return ClassDashClassCalendar(_FakeCoordinator(data), _FakeEntry(), "Physics")


def _data(
    due_soon=(), ahead=(), overdue=(), done=(), virtual=()
) -> ClassDashData:
    return ClassDashData(
        status={},
        due_soon=list(due_soon),
        ahead=list(ahead),
        overdue=list(overdue),
        done=list(done),
        announcements=[],
        classes=[],
        check_status={},
        virtual=list(virtual),
        update_status={},
    )


def test_event_is_the_soonest_not_yet_ended() -> None:
    data = _data(
        due_soon=[_assignment("Lab report", "2026-09-05T23:59:00+00:00", item_id="soon")],
        ahead=[_assignment("Final project", "2026-10-01T23:59:00+00:00", item_id="ahead")],
        overdue=[_assignment("Old worksheet", "2026-08-01T23:59:00+00:00", item_id="overdue")],
    )
    event = _calendar(data).event
    assert event is not None
    assert event.uid == "soon"
    assert event.summary == "Lab report"


def test_event_is_none_when_everything_is_over() -> None:
    data = _data(overdue=[_assignment("Old worksheet", "2020-01-01T23:59:00+00:00")])
    assert _calendar(data).event is None


def test_event_ignores_other_classes() -> None:
    data = _data(
        due_soon=[_assignment("Not physics", "2026-09-05T23:59:00+00:00", class_name="Biology")]
    )
    assert _calendar(data).event is None


async def test_async_get_events_includes_overdue_in_range() -> None:
    """Overdue items don't show up as `event`, but should still appear
    when a caller (e.g. the calendar UI, viewing a past week) asks for
    events in a range that covers them — this is the difference between
    "due-soon + ahead only" and "everything with a due date"."""
    data = _data(
        due_soon=[_assignment("Upcoming", "2026-09-05T23:59:00+00:00", item_id="upcoming")],
        overdue=[_assignment("Late", "2026-08-01T23:59:00+00:00", item_id="late")],
    )
    events = await _calendar(data).async_get_events(
        hass=None,
        start_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )
    assert {e.uid for e in events} == {"upcoming", "late"}


async def test_async_get_events_respects_the_range() -> None:
    data = _data(
        due_soon=[_assignment("In range", "2026-09-05T23:59:00+00:00", item_id="in")],
        ahead=[_assignment("Out of range", "2027-01-01T23:59:00+00:00", item_id="out")],
    )
    events = await _calendar(data).async_get_events(
        hass=None,
        start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        end_date=datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    assert {e.uid for e in events} == {"in"}


def test_overdue_items_are_tagged() -> None:
    data = _data(
        overdue=[_assignment("Worksheet", "2026-08-01T23:59:00+00:00", item_id="late")]
    )
    events = _calendar(data)._events()
    assert events[0].summary == "Worksheet (overdue)"


def test_done_items_appear_tagged() -> None:
    """Done items used to be excluded from the calendar entirely — they
    now show up like anything else, tagged so they're distinguishable
    from an undone item sharing the same due date."""
    data = _data(
        done=[_assignment("Lab report", "2026-09-05T23:59:00+00:00", item_id="done1")]
    )
    events = _calendar(data)._events()
    assert events[0].summary == "Lab report (done)"


def test_due_soon_and_ahead_are_not_tagged() -> None:
    data = _data(
        due_soon=[_assignment("Soon", "2026-09-05T23:59:00+00:00", item_id="soon")],
        ahead=[_assignment("Later", "2026-10-01T23:59:00+00:00", item_id="later")],
    )
    events = {e.uid: e.summary for e in _calendar(data)._events()}
    assert events == {"soon": "Soon", "later": "Later"}


def test_virtual_reminder_overdue_is_tagged() -> None:
    """Virtual reminders don't have their own overdue bucket the way real
    assignments do — a past due date makes one overdue instead, and it
    gets the same "(overdue)" tag for the same reason a real overdue
    assignment does."""
    data = _data(
        virtual=[_assignment("Reminder", "2020-01-01T23:59:00+00:00", item_id="v1")]
    )
    events = _calendar(data)._events()
    assert events[0].summary == "Reminder (overdue)"


def test_virtual_reminder_done_is_tagged_not_overdue() -> None:
    data = _data(
        virtual=[
            _assignment(
                "Reminder", "2020-01-01T23:59:00+00:00", item_id="v1", tags=["done"]
            )
        ]
    )
    events = _calendar(data)._events()
    assert events[0].summary == "Reminder (done)"


def test_hidden_items_excluded_regardless_of_tag() -> None:
    data = _data(
        overdue=[
            _assignment(
                "Dismissed", "2026-08-01T23:59:00+00:00", item_id="h1", tags=["hidden"]
            )
        ],
        done=[
            _assignment(
                "Dismissed done",
                "2026-09-05T23:59:00+00:00",
                item_id="h2",
                tags=["hidden"],
            )
        ],
    )
    assert _calendar(data)._events() == []
