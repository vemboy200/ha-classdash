"""Binary sensor entities for ClassDash — whether a check is running, and
whether today is a school day."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import ClassDashConfigEntry, ClassDashCoordinator
from .devices import main_device_info

# Reads the shared coordinator's pushed data — no per-entity network call.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClassDashConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [
            ClassDashCheckingBinarySensor(entry.runtime_data, entry),
            ClassDashSchoolTodayBinarySensor(entry.runtime_data, entry),
        ]
    )


class ClassDashCheckingBinarySensor(
    CoordinatorEntity[ClassDashCoordinator], BinarySensorEntity
):
    """On while ClassDash is checking Classroom/Canvas/Edpuzzle, from the
    automatic checks, Check now, Reload or ClassDash's own page alike."""

    _attr_has_entity_name = True
    _attr_translation_key = "checking"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.RUNNING
    _attr_icon = "mdi:book-search"

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_checking"
        self._attr_device_info = main_device_info(entry)

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.status["collecting"]


# ClassDash's own kind names, as the snake_case Home Assistant uses for
# attribute values.
DAY_KINDS = {"noSchool": "no_school", "minimumDay": "minimum_day"}


class ClassDashSchoolTodayBinarySensor(
    CoordinatorEntity[ClassDashCoordinator], BinarySensorEntity
):
    """On on a school day (minimum days included), by the school calendar
    in ClassDash's Settings → Calendar. Unknown without one."""

    _attr_has_entity_name = True
    _attr_translation_key = "school_today"
    _attr_icon = "mdi:school"

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_school_today"
        self._attr_device_info = main_device_info(entry)

    @property
    def is_on(self) -> bool | None:
        # From status rather than the calendar block: the heartbeat
        # refreshes status every minute, so this flips at midnight instead
        # of waiting for ClassDash's next real push.
        return self.coordinator.data.status["schoolToday"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        calendar = self.coordinator.data.calendar
        today = calendar["today"]
        return {
            "day_kind": DAY_KINDS.get(today["kind"]),
            "label": today["label"] or None,
            "events": today["events"],
            "next_school_day": calendar["nextSchoolDay"],
        }
