"""The ClassDash integration."""

from __future__ import annotations

import aiohttp

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import ClassDashClient, build_ssl_context
from .const import CONF_CERT_PEM
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator, class_names
from .devices import stale_class_devices
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.CALENDAR,
    Platform.BUTTON,
    Platform.UPDATE,
]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the hide/unhide/mute/unmute services.

    Domain-wide, not per config entry — see services.py's own comment.
    Runs once regardless of how many ClassDash entries get added later.
    """
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ClassDashConfigEntry) -> bool:
    """Set up ClassDash from a config entry."""
    ssl_context = await hass.async_add_executor_job(
        build_ssl_context, entry.data[CONF_CERT_PEM]
    )
    session: aiohttp.ClientSession = async_get_clientsession(hass)
    client = ClassDashClient(
        session,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
        ssl_context,
    )

    coordinator = ClassDashCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Classes change over time — a class that goes orphaned, stale, or
    # excluded in ClassDash itself should stop having a device here too,
    # not linger forever. sensor.py/calendar.py each handle *adding*
    # entities for a class that's newly showing up; this is the one place
    # that handles a class *disappearing*, since removing a device is a
    # registry-level operation that doesn't belong to either platform
    # specifically, and doing it once here avoids both platforms racing
    # to remove (or worse, disagreeing about) the same device.
    device_reg = dr.async_get(hass)

    @callback
    def _remove_stale_class_devices() -> None:
        if coordinator.data is None:
            return
        current = class_names(coordinator.data)
        for device_id, class_name in stale_class_devices(hass, entry, current):
            device_reg.async_remove_device(device_id)
            # So sensor.py/calendar.py know to re-add this class's
            # entities if it reappears later, instead of wrongly
            # thinking they're still there — see
            # ClassDashCoordinator.known_class_sensors' own docstring.
            coordinator.known_class_sensors.discard(class_name)
            coordinator.known_class_calendars.discard(class_name)

    _remove_stale_class_devices()
    entry.async_on_unload(coordinator.async_add_listener(_remove_stale_class_devices))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClassDashConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
