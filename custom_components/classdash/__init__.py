"""The ClassDash integration."""

from __future__ import annotations

import aiohttp

from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ClassDashClient, build_ssl_context
from .const import CONF_CERT_PEM
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CALENDAR, Platform.BUTTON]


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

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ClassDashConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
