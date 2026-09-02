"""Calendar entities for ClassDash — one per class.

Each class's due-soon, ahead, and overdue assignments become events on
that class's own calendar, keyed on the same due date/time ClassDash
already computed — plus any virtual reminder assigned to that class.
Announcements have no due date and aren't events.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .coordinator import (
    ClassDashConfigEntry,
    ClassDashCoordinator,
    class_names,
    is_done,
    is_hidden,
)
from .devices import class_device_info, class_unique_id

# Same reasoning as sensor.py's PARALLEL_UPDATES: everything here reads
# from the shared coordinator, nothing makes its own network call.
PARALLEL_UPDATES = 0

# A due date/time has no natural duration of its own — this just needs to
# be long enough that end > start (CalendarEvent requires it) and that the
# event is still findable as "current" for a little while after its due
# moment passes, without looking like it occupies real class time.
EVENT_DURATION = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClassDashConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one calendar per class, added as new classes show up."""
    coordinator = entry.runtime_data

    @callback
    def _add_new_class_calendars() -> None:
        """Tracked via coordinator.known_class_calendars, a fresh,
        process-local set — see that attribute's own docstring
        (ClassDashCoordinator, coordinator.py) for why this isn't an
        entity registry check: the registry persists a class's entities
        across a restart even though the actual Entity objects don't."""
        if coordinator.data is None:
            return
        new = class_names(coordinator.data) - coordinator.known_class_calendars
        if not new:
            return
        coordinator.known_class_calendars |= new
        async_add_entities(
            ClassDashClassCalendar(coordinator, entry, name) for name in sorted(new)
        )

    _add_new_class_calendars()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_class_calendars))


def _assignment_to_event(item: dict[str, Any], tag: str | None = None) -> CalendarEvent | None:
    due = dt_util.parse_datetime(item["due"]) if item.get("due") else None
    if due is None:
        return None
    title = item.get("title") or "Untitled assignment"
    return CalendarEvent(
        start=due,
        end=due + EVENT_DURATION,
        summary=f"{title} ({tag})" if tag else title,
        description=item.get("link") or "",
        uid=item.get("id"),
    )


class ClassDashClassCalendar(CoordinatorEntity[ClassDashCoordinator], CalendarEntity):
    """One class's assignments (due-soon + ahead + overdue) plus any
    virtual reminder assigned to it, as events."""

    _attr_has_entity_name = True
    _attr_translation_key = "assignments"
    _attr_icon = "mdi:calendar-check"

    def __init__(
        self,
        coordinator: ClassDashCoordinator,
        entry: ClassDashConfigEntry,
        class_name: str,
    ) -> None:
        super().__init__(coordinator)
        self._class_name = class_name
        self._attr_unique_id = f"{class_unique_id(entry, class_name)}_calendar"
        self._attr_device_info = class_device_info(entry, class_name)

    def _for_class(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            x for x in items if x.get("class") == self._class_name and not is_hidden(x)
        ]

    def _events(self) -> list[CalendarEvent]:
        """Due-soon/ahead/overdue/done, plus any virtual reminder assigned
        to this class. Done and overdue get a "(done)"/"(overdue)" tag on
        the title — due-soon/ahead don't need one, the due date alone
        already says when those are, but an overdue item is worth calling
        out plainly rather than making someone notice it's in the past on
        their own, and a done one would otherwise look identical to an
        undone one sharing the same due date.

        /api/virtual mixes every state (overdue/upcoming/undated/done)
        into one list, unlike real assignments where each state is
        already its own separate bucket — tagged here the same way, by
        checking is_done()/the due date directly, for the same reason a
        real assignment's state is worth tagging.
        """
        data = self.coordinator.data
        now = dt_util.now()
        events: list[CalendarEvent] = []

        for x in (*self._for_class(data.due_soon), *self._for_class(data.ahead)):
            if (e := _assignment_to_event(x)) is not None:
                events.append(e)

        for x in self._for_class(data.overdue):
            if (e := _assignment_to_event(x, tag="overdue")) is not None:
                events.append(e)

        for x in self._for_class(data.done):
            if (e := _assignment_to_event(x, tag="done")) is not None:
                events.append(e)

        for x in self._for_class(data.virtual):
            due = dt_util.parse_datetime(x["due"]) if x.get("due") else None
            if is_done(x):
                tag = "done"
            elif due is not None and due < now:
                tag = "overdue"
            else:
                tag = None
            if (e := _assignment_to_event(x, tag=tag)) is not None:
                events.append(e)

        events.sort(key=lambda e: e.start_datetime_local)
        return events

    @property
    def event(self) -> CalendarEvent | None:
        """The soonest event that hasn't ended yet — overdue assignments
        are still on the calendar (see async_get_events), just not
        reported here as "next"."""
        now = dt_util.now()
        for event in self._events():
            if event.end_datetime_local >= now:
                return event
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return [
            event
            for event in self._events()
            if event.start_datetime_local < end_date
            and event.end_datetime_local > start_date
        ]
