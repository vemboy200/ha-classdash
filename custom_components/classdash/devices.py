"""Device identity, shared across the sensor/calendar/button platforms.

A class's name is the only handle available across all three source
platforms (Classroom, Canvas, Edpuzzle) — see `class_names` in
coordinator.py for why /api/classes can't be used instead. Using it as
the device's stable identity means a renamed class becomes a new device;
accepted, there's nothing else to key on.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import ClassDashConfigEntry


def main_device_info(entry: ClassDashConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id)},
        name="ClassDash",
        manufacturer="ClassDash",
        model="School digest home API",
    )


def class_unique_id(entry: ClassDashConfigEntry, class_name: str) -> str:
    return f"{entry.unique_id}_class_{slugify(class_name)}"


def class_device_info(entry: ClassDashConfigEntry, class_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, class_unique_id(entry, class_name))},
        via_device=(DOMAIN, entry.unique_id),
        name=class_name,
        manufacturer="ClassDash",
        model="Class",
    )
