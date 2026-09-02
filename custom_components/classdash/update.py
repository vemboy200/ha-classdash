"""Update entity for ClassDash — the macOS app's own self-update check.

This tracks ClassDash's app version against GitHub, not this integration's
own version — see CONTRIBUTING.md's "Update check" section and
16-summary.swift's checkForUpdates() for the actual mechanism this reads.

Deliberately download-only: ClassDash's API can start downloading the
release .dmg in the background (async_download_update), but actually
installing it — replacing the running app and relaunching — stays gated
behind a native confirmation on the Mac itself, never reachable through
the API at all. async_install here only starts that download;
release_summary says so, since Home Assistant's own "Install" button would
otherwise look like it finishes the job.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ClassDashAuthError, ClassDashConnectionError
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator
from .devices import main_device_info

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClassDashConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([ClassDashAppUpdate(coordinator, entry)])


class ClassDashAppUpdate(CoordinatorEntity[ClassDashCoordinator], UpdateEntity):
    """ClassDash's own installed app version vs. the latest GitHub release."""

    _attr_has_entity_name = True
    _attr_translation_key = "app_update"
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL | UpdateEntityFeature.PROGRESS
    )
    _attr_release_summary = (
        "Only downloads the release in the background — actually "
        "installing it still needs confirming on the Mac itself, "
        "ClassDash's API can't do that part."
    )

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_app_update"
        self._attr_device_info = main_device_info(entry)

    @property
    def _status(self) -> dict[str, Any]:
        return self.coordinator.data.update_status

    @property
    def installed_version(self) -> str | None:
        return self._status.get("currentVersion")

    @property
    def latest_version(self) -> str | None:
        return self._status.get("latestVersion")

    @property
    def release_url(self) -> str | None:
        return self._status.get("url")

    @property
    def in_progress(self) -> bool | None:
        return self._status.get("downloading", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"ready_to_install": self._status.get("readyToInstall", False)}

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        try:
            await self.coordinator.client.async_download_update()
        except ClassDashAuthError as err:
            raise HomeAssistantError("ClassDash rejected the bearer token") from err
        except ClassDashConnectionError as err:
            raise HomeAssistantError(
                f"Could not reach ClassDash's home API: {err}"
            ) from err
        await self._async_refresh_status()

    async def _async_refresh_status(self) -> None:
        """update-status.json isn't watched by /api/stream (see
        ClassDashData.update_status's own docstring) — a write to it alone
        never triggers a push. Fetch it directly so this entity doesn't
        sit showing stale downloading/ready state until some unrelated
        collection pass happens to broadcast next. Best-effort: the
        download itself already started regardless of whether this
        follow-up fetch succeeds."""
        try:
            status = await self.coordinator.client.async_get_update_status()
        except (ClassDashAuthError, ClassDashConnectionError):
            return
        self.coordinator.async_set_updated_data(
            dataclasses.replace(self.coordinator.data, update_status=status)
        )
