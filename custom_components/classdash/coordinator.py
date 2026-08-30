"""Data update coordinator for ClassDash."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ClassDashAuthError, ClassDashClient, ClassDashConnectionError
from .const import DOMAIN, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass
class ClassDashData:
    """Everything a single poll gathers, bundled together."""

    status: dict[str, Any]
    due_soon: list[dict[str, Any]]
    ahead: list[dict[str, Any]]
    overdue: list[dict[str, Any]]
    announcements: list[dict[str, Any]]


type ClassDashConfigEntry = ConfigEntry[ClassDashCoordinator]


class ClassDashCoordinator(DataUpdateCoordinator[ClassDashData]):
    """Polls one ClassDash server on a fixed interval."""

    def __init__(
        self, hass: HomeAssistant, entry: ClassDashConfigEntry, client: ClassDashClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> ClassDashData:
        try:
            status, due_soon, ahead, overdue, announcements = await asyncio.gather(
                self.client.async_get_status(),
                self.client.async_get_due_soon(),
                self.client.async_get_ahead(),
                self.client.async_get_overdue(),
                self.client.async_get_announcements(),
            )
        except ClassDashAuthError as err:
            # The token was rolled on the server side (or was never right).
            # This tells HA to walk the user through reauth instead of just
            # failing silently update after update.
            raise ConfigEntryAuthFailed("bearer token rejected") from err
        except ClassDashConnectionError as err:
            raise UpdateFailed(str(err)) from err

        return ClassDashData(
            status=status,
            due_soon=due_soon,
            ahead=ahead,
            overdue=overdue,
            announcements=announcements,
        )
