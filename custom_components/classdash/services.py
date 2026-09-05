"""Custom service actions: hide/unhide/mute/unmute an assignment, plus
create/edit for virtual reminders.

These act on a specific assignment/reminder by id, not on an entity —
neither is its own HA entity (an assignment is a list item inside a
sensor's attribute; a reminder doesn't exist until created), so there's
no natural entity to attach an entity action to. Registered as plain
domain-wide services instead, resolved to a config entry either
automatically (the common single-server case) or via an explicit
`config_entry_id` when more than one is loaded.
"""

from __future__ import annotations

import dataclasses
import functools
from collections.abc import Coroutine
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    ClassDashValidationError,
)
from .const import DOMAIN
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator
from .devices import CLASS_DEVICE_MODEL

ATTR_CONFIG_ENTRY_ID = "config_entry_id"
ATTR_ID = "id"
ATTR_TITLE = "title"
ATTR_CLASS = "class"
ATTR_DUE = "due"

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_ID): cv.string,
    }
)

# class/due are always sent as a full replacement, not a partial diff —
# ClassDash's own edit() overwrites both every time, including clearing
# either back to null if left out — same as create, just with an id and
# a required title on top (create's title is also required; edit's just
# happens to matter more to call out, since it's easy to assume a blank
# field there means "leave unchanged").
#
# ATTR_CLASS carries a device_id, not a class name — services.yaml scopes
# its selector to this integration's own "Class"-model devices, so the UI
# only ever offers classes ClassDash currently tracks. A free-text class
# name here previously let a typo (or a class ClassDash doesn't track at
# all) spawn its own permanent phantom class device, since nothing outside
# a real collection pass ever tells this integration such a class doesn't
# really exist. _resolve_class_name below turns the selected device back
# into the plain name ClassDash's API actually wants.
_VIRTUAL_FIELDS = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Required(ATTR_TITLE): cv.string,
    vol.Optional(ATTR_CLASS): cv.string,
    vol.Optional(ATTR_DUE): cv.datetime,
}
CREATE_VIRTUAL_SCHEMA = vol.Schema(_VIRTUAL_FIELDS)
EDIT_VIRTUAL_SCHEMA = vol.Schema({**_VIRTUAL_FIELDS, vol.Required(ATTR_ID): cv.string})

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


async def _async_refresh_virtual(coordinator: ClassDashCoordinator) -> None:
    """virtual-assignments.json isn't one of the files /api/stream
    watches (see ClassDashClient.async_get_virtual's own docstring) —
    creating or editing a reminder never triggers its own push. Fetch
    the current list directly so the reminder's class/calendar reflects
    it right away instead of waiting on an unrelated real collection
    pass. Best-effort: the write itself already succeeded regardless of
    whether this follow-up fetch does."""
    try:
        virtual = await coordinator.client.async_get_virtual()
    except (ClassDashAuthError, ClassDashConnectionError):
        return
    coordinator.async_set_updated_data(
        dataclasses.replace(coordinator.data, virtual=virtual)
    )


async def _async_call_and_refresh(
    coordinator: ClassDashCoordinator, write: Coroutine[Any, Any, dict[str, Any]]
) -> ServiceResponse:
    """Runs a virtual-reminder write, translates its errors the same way
    every other service here does, then refreshes /api/virtual so the
    change shows up immediately. Returns the created/edited entry itself
    (unwrapped from ClassDash's own {"ok": true, "entry": {...}} shape)
    as the service's response — the only place a reminder's id is ever
    surfacable to a caller, since a reminder isn't its own HA entity and
    doesn't otherwise appear anywhere with its id visible."""
    try:
        result = await write
    except ClassDashValidationError as err:
        raise ServiceValidationError(str(err)) from err
    except ClassDashAuthError as err:
        raise HomeAssistantError("ClassDash rejected the bearer token") from err
    except ClassDashConnectionError as err:
        raise HomeAssistantError(f"Could not reach ClassDash's home API: {err}") from err
    await _async_refresh_virtual(coordinator)
    return result.get("entry")


def _resolve_class_name(
    hass: HomeAssistant, entry: ClassDashConfigEntry, device_id: str
) -> str:
    """Turn a class device (picked via the `class` field's device selector)
    back into the plain name ClassDash's own API expects — `device.name` is
    that name verbatim, same as `stale_class_devices` already relies on."""
    device = dr.async_get(hass).async_get(device_id)
    if (
        device is None
        or entry.entry_id not in device.config_entries
        or device.model != CLASS_DEVICE_MODEL
    ):
        raise ServiceValidationError(
            f"{device_id} isn't a ClassDash class device on this config entry"
        )
    return device.name


def _due_iso(call: ServiceCall) -> str | None:
    due = call.data.get(ATTR_DUE)
    # HA's own datetime selector/cv.datetime can hand back a naive
    # datetime (no offset) — treated as HA's own configured local time,
    # same as dt_util.as_utc always does, so the ISO string sent to
    # ClassDash is unambiguous regardless of what timezone the machine
    # actually running ClassDash's Node process happens to be in.
    return dt_util.as_utc(due).isoformat() if due else None


async def _async_create_virtual(call: ServiceCall) -> ServiceResponse:
    entry = _resolve_entry(call.hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    coordinator: ClassDashCoordinator = entry.runtime_data
    class_device_id = call.data.get(ATTR_CLASS)
    return await _async_call_and_refresh(
        coordinator,
        coordinator.client.async_create_virtual_reminder(
            title=call.data[ATTR_TITLE],
            class_name=(
                _resolve_class_name(call.hass, entry, class_device_id)
                if class_device_id
                else None
            ),
            due=_due_iso(call),
        ),
    )


async def _async_edit_virtual(call: ServiceCall) -> ServiceResponse:
    entry = _resolve_entry(call.hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    coordinator: ClassDashCoordinator = entry.runtime_data
    class_device_id = call.data.get(ATTR_CLASS)
    return await _async_call_and_refresh(
        coordinator,
        coordinator.client.async_edit_virtual_reminder(
            item_id=call.data[ATTR_ID],
            title=call.data[ATTR_TITLE],
            class_name=(
                _resolve_class_name(call.hass, entry, class_device_id)
                if class_device_id
                else None
            ),
            due=_due_iso(call),
        ),
    )


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the hide/unhide/mute/unmute and virtual-reminder services.

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

    hass.services.async_register(
        DOMAIN,
        "create_virtual_reminder",
        _async_create_virtual,
        schema=CREATE_VIRTUAL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "edit_virtual_reminder",
        _async_edit_virtual,
        schema=EDIT_VIRTUAL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
