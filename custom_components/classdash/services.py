"""Custom service actions: hide/unhide/mute/unmute an assignment.

These act on a specific assignment by id, not on an entity — assignments
aren't individual HA entities (they're list items inside a sensor's
attribute, discoverable via the `id` field added there specifically for
this), so there's no natural entity to attach an entity action to.
Registered as plain domain-wide services instead, resolved to a config
entry either automatically (the common single-server case) or via an
explicit `config_entry_id` when more than one is loaded.
"""

from __future__ import annotations

import functools

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import ClassDashAuthError, ClassDashConnectionError
from .const import DOMAIN
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ID = "id"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_ID): cv.string,
    }
)

# Service name -> ClassDashClient method name.
_SERVICES = {
    "hide": "async_hide",
    "unhide": "async_unhide",
    "mute": "async_mute",
    "unmute": "async_unmute",
}


def _resolve_entry(
    hass: HomeAssistant, config_entry_id: str | None
) -> ClassDashConfigEntry:
    """Pick which ClassDash server a service call targets.

    Most installs only have one, so config_entry_id is optional — with
    more than one loaded, the caller has to say which.
    """
    if config_entry_id is not None:
        entry = hass.config_entries.async_get_entry(config_entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise ServiceValidationError(
                f"No ClassDash config entry with id {config_entry_id}"
            )
        if entry.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError(
                f"ClassDash config entry {config_entry_id} is not loaded"
            )
        return entry

    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise ServiceValidationError("No loaded ClassDash config entry")
    if len(loaded) > 1:
        raise ServiceValidationError(
            "More than one ClassDash config entry is loaded — specify config_entry_id"
        )
    return loaded[0]


async def _async_handle(call: ServiceCall, method_name: str) -> None:
    entry = _resolve_entry(call.hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    coordinator: ClassDashCoordinator = entry.runtime_data
    method = getattr(coordinator.client, method_name)
    try:
        await method(call.data[ATTR_ID])
    except ClassDashAuthError as err:
        raise HomeAssistantError("ClassDash rejected the bearer token") from err
    except ClassDashConnectionError as err:
        raise HomeAssistantError(f"Could not reach ClassDash's home API: {err}") from err


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the hide/unhide/mute/unmute services.

    Called once from async_setup, not async_setup_entry — these aren't
    scoped to one config entry the way entities are (see the
    action-setup quality rule), and async_setup itself only ever runs
    once per domain for the lifetime of the HA process, regardless of
    how many config entries come and go afterward.
    """
    for service, method_name in _SERVICES.items():
        hass.services.async_register(
            DOMAIN,
            service,
            functools.partial(_async_handle, method_name=method_name),
            schema=SERVICE_SCHEMA,
        )
