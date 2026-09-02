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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MAX_LIST_ATTRIBUTES
from .coordinator import (
    ClassDashConfigEntry,
    ClassDashCoordinator,
    ClassDashData,
    class_names,
    is_hidden,
)
from .devices import class_device_info, class_unique_id, main_device_info

# Every entity here reads from the shared coordinator's already-fetched
# data — there's no per-entity network call for concurrency to matter to.
PARALLEL_UPDATES = 0


def _assignment_attrs(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Trim an assignment list down to what's worth showing as an attribute.

    Excludes hidden items — /api/due-soon etc. no longer filter those out
    server-side (everything goes out, tagged, per CONTRIBUTING.md), so
    without this the preview list would drift from the sensor's own count
    (which reads /api/status's already-filtered numbers). Includes `id`
    specifically so it's discoverable for the classdash.hide/mute
    services, which need it and have no other reasonable way to show it.
    """
    visible = [x for x in items if not is_hidden(x)]
    trimmed = [
        {
            "id": x.get("id"),
            "title": x.get("title"),
            "class": x.get("class"),
            "due": x.get("due"),
            "link": x.get("link"),
        }
        for x in visible[:MAX_LIST_ATTRIBUTES]
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
        key="done",
        translation_key="done",
        icon="mdi:book-check",
        native_unit_of_measurement="assignments",
        state_class="measurement",
        value_fn=lambda d: d.status["done"],
        attrs_fn=lambda d: _assignment_attrs(d.done),
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
    *(
        ClassDashSensorDescription(
            key=f"check_status_{platform}",
            translation_key=f"check_status_{platform}",
            icon=icon,
            device_class=SensorDeviceClass.ENUM,
            options=["ok", "problem", "unknown"],
            value_fn=lambda d, platform=platform: d.check_status[platform]["status"],
            attrs_fn=lambda d, platform=platform: {
                "at": d.check_status[platform]["at"],
                "detail": d.check_status[platform]["detail"],
            },
        )
        for platform, icon in (
            ("classroom", "mdi:google-classroom"),
            ("canvas", "mdi:school"),
            ("edpuzzle", "mdi:movie-play"),
        )
    ),
)


# The three per-class counts — one sensor each, on that class's own
# sub-device. Reuses the main sensors' translation keys/icons: a
# translation_key resolves per-domain, not per-device, so "due_soon" on a
# class device still shows as "Due soon", combined with the device's own
# name by has_entity_name.
CLASS_SENSOR_ICONS = {
    "due_soon": "mdi:book-clock",
    "overdue": "mdi:book-alert",
    "ahead": "mdi:book-clock-outline",
    "done": "mdi:book-check",
}


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

    ent_reg = er.async_get(hass)

    @callback
    def _add_new_class_sensors() -> None:
        """Give any class that doesn't already have sensors its three
        count sensors. Checks the entity registry directly rather than a
        locally-tracked "already added" set — a class whose device was
        removed by __init__.py's stale-device cleanup and then reappears
        needs to be re-added, and a set that only ever grows would
        wrongly think it's still there."""
        if coordinator.data is None:
            return
        new = {
            name
            for name in class_names(coordinator.data)
            if ent_reg.async_get_entity_id(
                "sensor", DOMAIN, f"{class_unique_id(entry, name)}_due_soon"
            )
            is None
        }
        if not new:
            return
        async_add_entities(
            ClassDashClassSensor(coordinator, entry, name, key)
            for name in sorted(new)
            for key in CLASS_SENSOR_ICONS
        )

    _add_new_class_sensors()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_class_sensors))


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
        self._attr_device_info = main_device_info(entry)

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)


class ClassDashClassSensor(CoordinatorEntity[ClassDashCoordinator], SensorEntity):
    """One class's own due-soon/overdue/ahead count, on its sub-device."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = "assignments"
    _attr_state_class = "measurement"

    def __init__(
        self,
        coordinator: ClassDashCoordinator,
        entry: ClassDashConfigEntry,
        class_name: str,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._class_name = class_name
        self._key = key
        self._attr_translation_key = key
        self._attr_icon = CLASS_SENSOR_ICONS[key]
        self._attr_unique_id = f"{class_unique_id(entry, class_name)}_{key}"
        self._attr_device_info = class_device_info(entry, class_name)

    def _items(self) -> list[dict[str, Any]]:
        return [
            x
            for x in getattr(self.coordinator.data, self._key)
            if x.get("class") == self._class_name and not is_hidden(x)
        ]

    @property
    def native_value(self) -> int:
        return len(self._items())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return _assignment_attrs(self._items())
