"""Sensor entities for ClassDash."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_LIST_ATTRIBUTES
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator, ClassDashData


def _assignment_attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Trim an assignment list down to what's worth showing as an attribute."""
    trimmed = [
        {
            "title": x.get("title"),
            "class": x.get("class"),
            "due": x.get("due"),
            "link": x.get("link"),
        }
        for x in items[:MAX_LIST_ATTRIBUTES]
    ]
    return {"assignments": trimmed}


def _announcement_attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    trimmed = [
        {
            "class": x.get("class"),
            "author": x.get("author"),
            "date": x.get("date"),
            "title": x.get("title"),
            "link": x.get("link"),
        }
        for x in items[:MAX_LIST_ATTRIBUTES]
    ]
    return {"announcements": trimmed}


@dataclass(frozen=True, kw_only=True)
class ClassDashSensorDescription(SensorEntityDescription):
    """Describes one ClassDash sensor."""

    value_fn: Callable[[ClassDashData], Any]
    attrs_fn: Callable[[ClassDashData], dict[str, Any]] | None = None


SENSOR_DESCRIPTIONS: tuple[ClassDashSensorDescription, ...] = (
    ClassDashSensorDescription(
        key="due_soon",
        translation_key="due_soon",
        icon="mdi:book-clock",
        native_unit_of_measurement="assignments",
        state_class="measurement",
        value_fn=lambda d: d.status["dueSoon"],
        attrs_fn=lambda d: _assignment_attrs(d.due_soon),
    ),
    ClassDashSensorDescription(
        key="overdue",
        translation_key="overdue",
        icon="mdi:book-alert",
        native_unit_of_measurement="assignments",
        state_class="measurement",
        value_fn=lambda d: d.status["overdue"],
        attrs_fn=lambda d: _assignment_attrs(d.overdue),
    ),
    ClassDashSensorDescription(
        key="ahead",
        translation_key="ahead",
        icon="mdi:book-clock-outline",
        native_unit_of_measurement="assignments",
        state_class="measurement",
        value_fn=lambda d: d.status["ahead"],
        attrs_fn=lambda d: _assignment_attrs(d.ahead),
    ),
    ClassDashSensorDescription(
        key="announcements",
        translation_key="announcements",
        icon="mdi:bullhorn",
        native_unit_of_measurement="posts",
        state_class="measurement",
        value_fn=lambda d: d.status["announcements"],
        attrs_fn=lambda d: _announcement_attrs(d.announcements),
    ),
    ClassDashSensorDescription(
        key="classes",
        translation_key="classes",
        icon="mdi:google-classroom",
        native_unit_of_measurement="classes",
        state_class="measurement",
        value_fn=lambda d: d.status["classes"],
    ),
    ClassDashSensorDescription(
        key="last_collected",
        translation_key="last_collected",
        icon="mdi:clock-check-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d: dt_util.parse_datetime(d.status["collectedAt"])
        if d.status["collectedAt"]
        else None,
        attrs_fn=lambda d: {"minutes_ago": d.status["minutesAgo"]},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClassDashConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up ClassDash sensors from a config entry."""
    coordinator = entry.runtime_data
    async_add_entities(
        ClassDashSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class ClassDashSensor(CoordinatorEntity[ClassDashCoordinator], SensorEntity):
    """A single ClassDash-derived sensor."""

    entity_description: ClassDashSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ClassDashCoordinator,
        entry: ClassDashConfigEntry,
        description: ClassDashSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name="ClassDash",
            manufacturer="ClassDash",
            model="School digest home API",
        )

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
