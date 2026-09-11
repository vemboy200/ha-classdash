"""Todo entity for ClassDash — one combined checklist across every real
assignment (due-soon/ahead/overdue/done) and virtual reminder, on the
main device.

Checking an item off writes back to ClassDash, not just to Home
Assistant — but what that write actually is differs by source, since
ClassDash's own write API isn't symmetric between the two:

- A virtual reminder has a real, writable done/undone state
  (/api/virtual/done, /api/virtual/undone) — checking one off there maps
  directly onto it.
- A real assignment does NOT — its "done" bucket is purely server-
  detected (Classroom/Canvas telling ClassDash it was actually
  submitted), there's no API call that marks one done. hide/unhide are
  the only writes available for one of these, so checking one off here
  hides it instead — the closest real analog ClassDash offers to "I'm
  done with this," and the same action classdash.hide already wraps.
  Un-checking a real assignment that's ALREADY done this way (genuinely
  submitted, not just hidden) has nothing to call — there's no
  "un-submit" — so that's a no-op check, not an error, same reasoning
  the update entity's own gaps get documented rather than pretended
  fixable.

Hidden items (real or virtual) are excluded entirely, same as
everywhere else in this integration.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import ClassDashAuthError, ClassDashConnectionError
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator, is_done, is_hidden
from .devices import main_device_info

PARALLEL_UPDATES = 0

# Matches 24-virtual-assignments.js's own create() ('v-' + 8 random
# bytes hex) — the only way this integration has to tell a virtual
# reminder's id apart from a real assignment's (a Classroom/Canvas/
# Edpuzzle internal id, never in this shape).
_VIRTUAL_ID_PREFIX = "v-"


def _is_virtual_id(item_id: str) -> bool:
    return item_id.startswith(_VIRTUAL_ID_PREFIX)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ClassDashConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([ClassDashTodoList(coordinator, entry)])


def _to_todo_item(item: dict[str, Any], *, done: bool) -> TodoItem:
    due = dt_util.parse_datetime(item["due"]) if item.get("due") else None
    class_name = item.get("class")
    summary = f"{item['title']} ({class_name})" if class_name else item["title"]
    return TodoItem(
        uid=item["id"],
        summary=summary,
        status=TodoItemStatus.COMPLETED if done else TodoItemStatus.NEEDS_ACTION,
        due=due,
    )


class ClassDashTodoList(CoordinatorEntity[ClassDashCoordinator], TodoListEntity):
    """Every real assignment plus every virtual reminder, as one checklist."""

    _attr_has_entity_name = True
    _attr_translation_key = "todo_list"
    _attr_icon = "mdi:checkbox-marked-outline"
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(
        self, coordinator: ClassDashCoordinator, entry: ClassDashConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.unique_id}_todo"
        self._attr_device_info = main_device_info(entry)

    @property
    def todo_items(self) -> list[TodoItem]:
        data = self.coordinator.data
        items = [
            _to_todo_item(x, done=(key == "done"))
            for key in ("due_soon", "ahead", "overdue", "done")
            for x in getattr(data, key)
            if not is_hidden(x)
        ]
        items += [
            _to_todo_item(x, done=is_done(x)) for x in data.virtual if not is_hidden(x)
        ]
        return items

    async def async_update_todo_item(self, item: TodoItem) -> None:
        assert item.uid is not None
        completed = item.status == TodoItemStatus.COMPLETED
        client = self.coordinator.client
        try:
            if _is_virtual_id(item.uid):
                if completed:
                    await client.async_mark_virtual_done(item.uid)
                else:
                    await client.async_unmark_virtual_done(item.uid)
                # Same gap as create/edit: virtual-assignments.json isn't
                # watched by /api/stream, so nothing else would tell this
                # entity the change happened — refetch directly.
                virtual = await client.async_get_virtual()
                self.coordinator.async_set_updated_data(
                    dataclasses.replace(self.coordinator.data, virtual=virtual)
                )
            elif completed:
                await client.async_hide(item.uid)
            else:
                await client.async_unhide(item.uid)
        except ClassDashAuthError as err:
            raise HomeAssistantError("ClassDash rejected the bearer token") from err
        except ClassDashConnectionError as err:
            raise HomeAssistantError(
                f"Could not reach ClassDash's home API: {err}"
            ) from err
