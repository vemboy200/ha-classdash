"""Tests for setting up and unloading a ClassDash config entry."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.classdash.api import ClassDashAuthError, StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

FAKE_STATUS = {
    "collectedAt": "2026-08-30T12:00:00.000Z",
    "minutesAgo": 3,
    "classes": 6,
    "total": 12,
    "dueSoon": 2,
    "overdue": 1,
    "ahead": 9,
    "done": 5,
    "announcements": 4,
    "removed": 0,
    "language": "en",
}
FAKE_BUNDLE = {
    **bundle_extras(),
    "status": FAKE_STATUS,
    # due_soon's own sensor now counts this list directly (plus any
    # matching virtual reminder) rather than trusting status["dueSoon"]
    # blindly — see sensor.py's _bucket_items — so this needs to actually
    # hold 2 items to match FAKE_STATUS's dueSoon: 2 above.
    "due-soon": [
        {"id": "a1", "title": "Reading", "class": "Physics", "due": None, "tags": []},
        {"id": "a2", "title": "Worksheet", "class": "Physics", "due": None, "tags": []},
    ],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [],
}


def _make_entry(sample_certificate) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={
            "host": "192.168.1.50",
            "port": 8734,
            "token": "a" * 64,
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )


async def _open_stream_stub():
    """A stream that pushes one snapshot and then stays connected."""
    yield StreamEvent("update", FAKE_BUNDLE)
    await asyncio.Event().wait()


async def test_setup_and_unload_entry(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = _open_stream_stub

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED

        due_soon = hass.states.get("sensor.classdash_due_soon")
        assert due_soon is not None
        assert due_soon.state == "2"

        last_collected = hass.states.get("sensor.classdash_last_collected")
        assert last_collected is not None
        assert last_collected.attributes["minutes_ago"] == 3

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_entry_triggers_reauth_on_bad_token(
    hass: HomeAssistant, sample_certificate
) -> None:
    """A bad/expired token on the very first connection starts reauth.
    Setup itself succeeds, since it no longer waits for ClassDash."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    async def failing_stream():
        raise ClassDashAuthError("bad token")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = failing_stream

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        async with asyncio.timeout(2):
            while not any(
                flow["context"].get("source") == "reauth"
                for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            ):
                await asyncio.sleep(0)


async def test_setup_succeeds_while_classdash_is_offline(
    hass: HomeAssistant, sample_certificate
) -> None:
    """ClassDash being off when Home Assistant starts (a shut-down laptop)
    doesn't block setup: the entry loads, entities are unavailable, and
    they fill in once ClassDash answers."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)
    online = asyncio.Event()

    async def stream():
        await online.wait()
        yield StreamEvent("update", FAKE_BUNDLE)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = stream

        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("sensor.classdash_due_soon").state == "unavailable"
        assert hass.states.get("todo.classdash_to_do_list").state == "unavailable"

        online.set()
        async with asyncio.timeout(2):
            while hass.states.get("sensor.classdash_due_soon").state != "2":
                await asyncio.sleep(0)

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
