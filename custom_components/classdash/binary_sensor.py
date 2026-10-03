"""Binary sensor entities for ClassDash — whether a check is running."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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
    async_add_entities([ClassDashCheckingBinarySensor(entry.runtime_data, entry)])


class ClassDashCheckingBinarySensor(
    CoordinatorEntity[ClassDashCoordinator], BinarySensorEntity
):
    """On while ClassDash is checking Classroom/Canvas/Edpuzzle, from the
    automatic checks, Check now, Reload or ClassDash's own page alike."""

    _attr_has_entity_name = True
    _attr_translation_key = "checking"
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
