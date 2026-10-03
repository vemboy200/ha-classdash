"""Tests for assignment details: teacher, locked and linked, as attributes
and in to-do item and calendar event descriptions."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant

from custom_components.classdash.coordinator import assignment_details


@pytest.fixture(autouse=True)
async def utc(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("UTC")


def _item(**fields) -> dict:
    return {"id": "a1", "title": "Lab report", "link": None, **fields}


@pytest.mark.parametrize(
    ("locked", "line"),
    [
        ({"why": "opens", "at": "2026-12-10T08:00:00.000Z", "module": None}, "Locked until Dec 10, 08:00"),
        ({"why": "module", "at": None, "module": "Unit 3"}, "Locked until Unit 3 is done"),
        ({"why": "module", "at": None, "module": None}, "Locked until a module is done"),
        ({"why": "closed", "at": "2026-09-30T23:59:00.000Z", "module": None}, "Closed Sep 30, 23:59"),
        ({"why": "locked", "at": None, "module": None}, "Locked"),
    ],
)
async def test_locked(locked: dict, line: str) -> None:
    assert assignment_details(_item(locked=locked)) == line


async def test_all_details_one_per_line() -> None:
    item = _item(
        teacher="Ms. Frizzle",
        locked=None,
        linked={"done": 1, "total": 2, "parts": []},
        link="https://classroom.google.com/c/1",
    )
    assert assignment_details(item).split("\n") == [
        "Teacher: Ms. Frizzle",
        "Linked: 1 of 2 parts done",
        "https://classroom.google.com/c/1",
    ]


async def test_nothing_to_say_is_empty() -> None:
    # A virtual reminder: none of the fields at all.
    assert assignment_details({"id": "v-1", "title": "Study", "link": None}) == ""
    assert assignment_details(_item(teacher=None, locked=None, linked=None)) == ""
