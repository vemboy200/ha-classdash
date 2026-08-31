"""Button entities for ClassDash — Reload and Check now, on the main device.

Both just start a collection pass and return — CONTRIBUTING.md is explicit
that neither endpoint waits for the pass to finish. There's nothing to
poll for here: the push stream's next "update" event (or "Last collected"
ticking forward) is how you'd notice it happened, same as pressing the
equivalent button on ClassDash's own summary page.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
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
    async_add_entities(
        [
            ClassDashReloadButton(coordinator, entry),
            ClassDashCheckButton(coordinator, entry),
        ]
    )


class _ClassDashActionButton(CoordinatorEntity[ClassDashCoordinator], ButtonEntity):
    """Shared plumbing; each subclass only supplies which call to make."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = main_device_info(entry)

    async def _async_call(self) -> None:
        raise NotImplementedError

    async def async_press(self) -> None:
        try:
            await self._async_call()
        except ClassDashConnectionError as err:
            raise HomeAssistantError(
                f"Could not reach ClassDash's home API: {err}"
            ) from err
        except ClassDashAuthError as err:
            # Reauth, if the token really has changed, is the running
            # stream connection's job (coordinator.py) — this just needs
            # the button press itself to fail visibly rather than silently.
            raise HomeAssistantError(
                "ClassDash rejected the bearer token"
            ) from err


class ClassDashReloadButton(_ClassDashActionButton):
    _attr_icon = "mdi:refresh"

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "reload")

    async def _async_call(self) -> None:
        await self.coordinator.client.async_reload()


class ClassDashCheckButton(_ClassDashActionButton):
    _attr_icon = "mdi:refresh-circle"

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry
    ) -> None:
        super().__init__(coordinator, entry, "check")

    async def _async_call(self) -> None:
        await self.coordinator.client.async_check()
