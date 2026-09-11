"""Tests for the combined todo entity."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.todo import TodoItemStatus
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

BASE_STATUS = {
    "collectedAt": "2026-09-01T00:00:00.000Z",
    "minutesAgo": 1,
    "classes": 0,
    "total": 0,
    "dueSoon": 0,
    "overdue": 0,
    "ahead": 0,
    "done": 0,
    "announcements": 0,
    "removed": 0,
    "language": "en",
}


def _assignment(class_name: str, title: str, due: str | None, item_id: str, tags=()) -> dict:
    return {
        "id": item_id,
        "title": title,
        "class": class_name,
        "due": due,
        "link": None,
        "tags": list(tags),
    }


def _bundle(
    due_soon=(), ahead=(), overdue=(), done=(), virtual=()
) -> dict:
    return {
        **bundle_extras(),
        "status": BASE_STATUS,
        "due-soon": list(due_soon),
        "ahead": list(ahead),
        "overdue": list(overdue),
        "done": list(done),
        "announcements": [],
        "classes": [],
        "virtual": list(virtual),
    }


async def _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={
            "host": "192.168.1.50",
            "port": 8734,
            "token": "a" * 64,
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )
    entry.add_to_hass(hass)

    async def fake_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    mock_client_cls.return_value.async_stream_updates = fake_stream
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant) -> str:
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("todo", DOMAIN, "192.168.1.50:8734_todo")
    assert entity_id is not None
    return entity_id


async def test_combines_real_and_virtual_items(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-10-01T00:00:00+00:00", "p1")],
        overdue=[_assignment("Physics", "Old worksheet", "2026-09-01T00:00:00+00:00", "o1")],
        done=[_assignment("Physics", "Finished", "2026-08-01T00:00:00+00:00", "d1")],
        virtual=[
            _assignment(
                "Biology", "Study notes", "2026-10-05T00:00:00+00:00", "v-aaa11111"
            ),
            _assignment(
                None, "Ask about extra credit", None, "v-bbb22222", tags=["done"]
            ),
        ],
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        state = hass.states.get(_entity_id(hass))

    assert state is not None
    # 5 items total, 3 not-yet-done (state = count of NEEDS_ACTION items).
    assert state.state == "3"
    assert state.attributes["supported_features"] == 4  # UPDATE_TODO_ITEM only


async def test_todo_items_have_correct_status_and_summary(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-10-01T00:00:00+00:00", "p1")],
        done=[_assignment("Physics", "Finished", "2026-08-01T00:00:00+00:00", "d1")],
        virtual=[
            _assignment(
                "Biology", "Study notes", "2026-10-05T00:00:00+00:00", "v-aaa11111"
            ),
            _assignment(None, "No class here", None, "v-bbb22222"),
        ],
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        entry = await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity = hass.data["entity_components"]["todo"].get_entity(_entity_id(hass))
        items = {item.uid: item for item in entity.todo_items}

    assert items["p1"].summary == "Lab report (Physics)"
    assert items["p1"].status == TodoItemStatus.NEEDS_ACTION
    assert items["d1"].status == TodoItemStatus.COMPLETED
    assert items["v-aaa11111"].summary == "Study notes (Biology)"
    assert items["v-aaa11111"].status == TodoItemStatus.NEEDS_ACTION
    assert items["v-bbb22222"].summary == "No class here"
    assert items["v-bbb22222"].due is None


async def test_hidden_items_excluded(hass: HomeAssistant, sample_certificate) -> None:
    bundle = _bundle(
        due_soon=[
            _assignment(
                "Physics",
                "Dismissed",
                "2026-10-01T00:00:00+00:00",
                "p1",
                tags=["hidden"],
            )
        ],
        virtual=[
            _assignment(
                "Physics",
                "Dismissed reminder",
                "2026-10-01T00:00:00+00:00",
                "v-aaa11111",
                tags=["hidden"],
            )
        ],
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity = hass.data["entity_components"]["todo"].get_entity(_entity_id(hass))

    assert entity.todo_items == []


async def test_checking_off_real_assignment_calls_hide(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-10-01T00:00:00+00:00", "p1")]
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_hide = AsyncMock()
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": entity_id, "item": "p1", "status": "completed"},
            blocking=True,
        )

        mock_client_cls.return_value.async_hide.assert_called_once_with("p1")


async def test_unchecking_real_assignment_calls_unhide(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        done=[_assignment("Physics", "Finished", "2026-08-01T00:00:00+00:00", "d1")]
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_unhide = AsyncMock()
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": entity_id, "item": "d1", "status": "needs_action"},
            blocking=True,
        )

        mock_client_cls.return_value.async_unhide.assert_called_once_with("d1")


async def test_checking_off_virtual_reminder_calls_mark_done_and_refreshes(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        virtual=[
            _assignment(
                "Biology", "Study notes", "2026-10-05T00:00:00+00:00", "v-aaa11111"
            )
        ]
    )
    updated = [
        _assignment(
            "Biology",
            "Study notes",
            "2026-10-05T00:00:00+00:00",
            "v-aaa11111",
            tags=["done"],
        )
    ]
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_mark_virtual_done = AsyncMock()
        mock_client_cls.return_value.async_get_virtual = AsyncMock(return_value=updated)
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": entity_id, "item": "v-aaa11111", "status": "completed"},
            blocking=True,
        )

        mock_client_cls.return_value.async_mark_virtual_done.assert_called_once_with(
            "v-aaa11111"
        )
        mock_client_cls.return_value.async_get_virtual.assert_called_once()
        state = hass.states.get(entity_id)
        # Refetch already reflects the done tag — the item shouldn't
        # still count toward the "needs action" state.
        assert state.state == "0"


async def test_unchecking_virtual_reminder_calls_unmark_done(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        virtual=[
            _assignment(
                "Biology",
                "Study notes",
                "2026-10-05T00:00:00+00:00",
                "v-aaa11111",
                tags=["done"],
            )
        ]
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_unmark_virtual_done = AsyncMock()
        mock_client_cls.return_value.async_get_virtual = AsyncMock(
            return_value=bundle["virtual"]
        )
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "todo",
            "update_item",
            {"entity_id": entity_id, "item": "v-aaa11111", "status": "needs_action"},
            blocking=True,
        )

        mock_client_cls.return_value.async_unmark_virtual_done.assert_called_once_with(
            "v-aaa11111"
        )


async def test_update_surfaces_auth_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-10-01T00:00:00+00:00", "p1")]
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_hide = AsyncMock(
            side_effect=ClassDashAuthError("bad token")
        )
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "todo",
                "update_item",
                {"entity_id": entity_id, "item": "p1", "status": "completed"},
                blocking=True,
            )


async def test_update_surfaces_connection_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    bundle = _bundle(
        virtual=[
            _assignment(
                "Biology", "Study notes", "2026-10-05T00:00:00+00:00", "v-aaa11111"
            )
        ]
    )
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_mark_virtual_done = AsyncMock(
            side_effect=ClassDashConnectionError("refused")
        )
        await _setup_with_bundle(hass, sample_certificate, mock_client_cls, bundle)
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "todo",
                "update_item",
                {"entity_id": entity_id, "item": "v-aaa11111", "status": "completed"},
                blocking=True,
            )
