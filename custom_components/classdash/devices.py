"""Device identity, shared across the sensor/calendar/button platforms.

A class's name is the only handle available across all three source
platforms (Classroom, Canvas, Edpuzzle) — see `class_names` in
coordinator.py for why /api/classes can't be used instead. Using it as
the device's stable identity means a renamed class becomes a new device;
accepted, there's nothing else to key on.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from .const import DOMAIN
from .coordinator import ClassDashConfigEntry, ClassDashData


def main_device_info(entry: ClassDashConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.unique_id)},
        name="ClassDash",
        manufacturer="ClassDash",
        model="School digest home API",
    )


# Shared with services.py, which filters a device selector on it to offer
# only real class devices (not the main "ClassDash" device) when picking a
# class for a virtual reminder.
CLASS_DEVICE_MODEL = "Class"


def class_unique_id(entry: ClassDashConfigEntry, class_name: str) -> str:
    return f"{entry.unique_id}_class_{slugify(class_name)}"


def class_device_details(data: ClassDashData, class_name: str) -> dict[str, str | None]:
    """The parts of a class device's info that come from ClassDash and can
    change: the teacher, shown as the manufacturer ("Class by <teacher>"),
    and the platform it's on as the hardware version, or "Linked" for
    classes linked in ClassDash's Settings → Classes. A class /api/classes
    doesn't list (announcements only) or whose platform didn't name a
    teacher falls back to ClassDash.
    """
    roster = next((c for c in data.classes if c["name"] == class_name), None)
    if roster is None:
        return {"manufacturer": "ClassDash", "hw_version": None}
    # `classes` is the real platform classes behind the entry: more than
    # one only when it's a link. `platform` is a list when the class spans
    # more than one, which without a link means two platforms happen to
    # use the same name.
    platform = roster["platform"]
    if len(roster["classes"]) > 1:
        hw_version = "Linked"
    elif isinstance(platform, list):
        hw_version = ", ".join(platform)
    else:
        hw_version = platform
    return {"manufacturer": roster["teacher"] or "ClassDash", "hw_version": hw_version}


def class_device_info(entry: ClassDashConfigEntry, class_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, class_unique_id(entry, class_name))},
        via_device_id=entry.runtime_data.main_device_id,
        name=class_name,
        model=CLASS_DEVICE_MODEL,
        **class_device_details(entry.runtime_data.data, class_name),
    )


def stale_class_devices(
    hass: HomeAssistant, entry: ClassDashConfigEntry, current_names: set[str]
) -> list[tuple[str, str]]:
    """(device id, class name) pairs for class sub-devices that no longer
    correspond to any name in `current_names` — a class that's become
    orphaned, gone stale, or been excluded in ClassDash itself.

    Identifies a "class sub-device" by `via_device_id` pointing at the
    main device, rather than trying to parse a name back out of its
    slugified identifier (not reliably invertible) — comparing identifier
    sets directly instead. The class name is `device.name` itself, not
    reconstructed from the identifier either — class_device_info sets it
    to the plain class name directly, so it's already right there. The
    caller needs the name (not just the device id) to also drop it from
    ClassDashCoordinator.known_class_sensors/known_class_calendars.
    """
    device_reg = dr.async_get(hass)
    main_device_id = entry.runtime_data.main_device_id
    current_identifiers = {
        (DOMAIN, class_unique_id(entry, name)) for name in current_names
    }
    return [
        (device.id, device.name)
        for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id)
        if device.via_device_id == main_device_id
        and not (device.identifiers & current_identifiers)
    ]
