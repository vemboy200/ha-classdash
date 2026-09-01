"""Calendar entities for ClassDash — one per class.

Each class's due-soon, ahead, and overdue assignments become events on
that class's own calendar, keyed on the same due date/time ClassDash
already computed. Announcements have no due date and aren't events.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator, class_names, is_hidden
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
    ent_reg = er.async_get(hass)

    @callback
    def _add_new_class_calendars() -> None:
        """Checks the entity registry directly rather than a locally
        tracked set — see sensor.py's own _add_new_class_sensors for why:
        a class whose device got removed and later reappears needs to be
        re-added, which an add-only set would miss."""
        if coordinator.data is None:
            return
        new = {
            name
            for name in class_names(coordinator.data)
            if ent_reg.async_get_entity_id(
                "calendar", DOMAIN, f"{class_unique_id(entry, name)}_calendar"
            )
            is None
        }
        if not new:
            return
        async_add_entities(
            ClassDashClassCalendar(coordinator, entry, name) for name in sorted(new)
        )

    _add_new_class_calendars()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_class_calendars))


def _assignment_to_event(item: dict[str, Any]) -> CalendarEvent | None:
    due = dt_util.parse_datetime(item["due"]) if item.get("due") else None
    if due is None:
        return None
    return CalendarEvent(
        start=due,
        end=due + EVENT_DURATION,
        summary=item.get("title") or "Untitled assignment",
        description=item.get("link") or "",
        uid=item.get("id"),
    )


class ClassDashClassCalendar(CoordinatorEntity[ClassDashCoordinator], CalendarEntity):
    """One class's assignments (due-soon + ahead + overdue), as events."""

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

    def _events(self) -> list[CalendarEvent]:
        data = self.coordinator.data
        items = [
            x
            for x in (*data.due_soon, *data.ahead, *data.overdue)
            if x.get("class") == self._class_name and not is_hidden(x)
        ]
        events = [e for x in items if (e := _assignment_to_event(x)) is not None]
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
