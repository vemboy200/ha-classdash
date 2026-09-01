"""Diagnostics for ClassDash.

Deliberately narrow about what gets included: this integration handles a
minor's school data (assignment titles, teacher names, links), and a
diagnostics dump is the kind of thing that ends up pasted into a public
GitHub issue. Counts, class names, and coordinator/connection state cover
what's actually useful for debugging this integration's own logic
(missing entities, wrong counts, a stuck connection) without dragging
along the content of what's actually due.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_TOKEN
from homeassistant.core import HomeAssistant

from .const import CONF_CERT_PEM
from .coordinator import ClassDashConfigEntry

TO_REDACT = {CONF_TOKEN, CONF_CERT_PEM}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ClassDashConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data

    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "last_exception": repr(coordinator.last_exception)
            if coordinator.last_exception
            else None,
            "status": data.status if data else None,
            "check_status": data.check_status if data else None,
            "classes": data.classes if data else None,
            "counts": {
                "due_soon": len(data.due_soon),
                "ahead": len(data.ahead),
                "overdue": len(data.overdue),
                "done": len(data.done),
                "announcements": len(data.announcements),
                "virtual": len(data.virtual),
            }
            if data
            else None,
        },
    }
