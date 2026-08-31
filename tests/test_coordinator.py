"""Tests for the push-based coordinator's stream handling.

Covers the parts test_init.py's end-to-end setup test doesn't reach:
a second, later push actually updating coordinator.data without a poll,
reconnecting after a dropped connection, reauth triggering from *inside*
the running stream (not just at initial setup), and that a first-connect
timeout doesn't leave the listener task running forever.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.coordinator import ClassDashCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

BUNDLE_1 = {
    "status": {"dueSoon": 1, "overdue": 0, "ahead": 0, "announcements": 0},
    "due-soon": [{"title": "first"}],
    "ahead": [],
    "overdue": [],
    "announcements": [],
}
BUNDLE_2 = {
    "status": {"dueSoon": 2, "overdue": 0, "ahead": 0, "announcements": 0},
    "due-soon": [{"title": "first"}, {"title": "second"}],
    "ahead": [],
    "overdue": [],
    "announcements": [],
}


@pytest.fixture
def entry(hass: HomeAssistant, sample_certificate) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={
            "host": "192.168.1.50",
            "port": 8734,
            "token": "a" * 64,
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )
    e.add_to_hass(hass)
    # async_config_entry_first_refresh asserts this state — normally set by
    # the config entries machinery mid-setup; these tests call it directly.
    e.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)
    return e


def _make_coordinator(hass: HomeAssistant, entry: MockConfigEntry, stream_fn):
    client = AsyncMock()
    client.async_stream_updates = stream_fn
    return ClassDashCoordinator(hass, entry, client)


async def test_subsequent_push_updates_coordinator_data(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A second event, arriving after setup, reaches coordinator.data with
    no polling involved — this is the whole point of local_push."""
    release_second = asyncio.Event()
    second_pushed = asyncio.Event()

    async def fake_stream():
        yield StreamEvent("update", BUNDLE_1)
        await release_second.wait()
        yield StreamEvent("update", BUNDLE_2)
        second_pushed.set()
        await asyncio.Event().wait()  # keep the "connection" open

    coordinator = _make_coordinator(hass, entry, fake_stream)
    try:
        await coordinator.async_config_entry_first_refresh()
        assert coordinator.data.status["dueSoon"] == 1

        release_second.set()
        # Background tasks are deliberately excluded from
        # hass.async_block_till_done(), so wait on the push directly.
        await asyncio.wait_for(second_pushed.wait(), timeout=2)

        assert coordinator.data.status["dueSoon"] == 2
        assert coordinator.last_update_success is True
    finally:
        coordinator._listen_task.cancel()


async def test_heartbeat_refreshes_status_without_touching_lists(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A heartbeat carries only /api/status — it should refresh that (so
    minutes_ago actually ticks over time) without replacing the
    assignment/announcement lists, which it doesn't even carry."""
    heartbeat_pushed = asyncio.Event()

    heartbeat_status = {**BUNDLE_1["status"], "minutesAgo": 3}

    async def fake_stream():
        yield StreamEvent("update", BUNDLE_1)
        yield StreamEvent("heartbeat", heartbeat_status)
        heartbeat_pushed.set()
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    try:
        await coordinator.async_config_entry_first_refresh()
        await asyncio.wait_for(heartbeat_pushed.wait(), timeout=2)

        assert coordinator.data.status == heartbeat_status
        # Untouched — a heartbeat carries no list data at all.
        assert coordinator.data.due_soon == BUNDLE_1["due-soon"]
        assert coordinator.last_update_success is True
    finally:
        coordinator._listen_task.cancel()


async def test_heartbeat_before_any_update_is_ignored(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Shouldn't happen in practice (ClassDash always sends "update"
    immediately on connect, well under the 60s heartbeat interval), but
    a heartbeat arriving with no data yet must not crash first refresh."""

    async def fake_stream():
        yield StreamEvent("heartbeat", {"dueSoon": 0})
        yield StreamEvent("update", BUNDLE_1)
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    try:
        await coordinator.async_config_entry_first_refresh()
        assert coordinator.data.status["dueSoon"] == 1
    finally:
        coordinator._listen_task.cancel()


async def test_reconnects_after_connection_error_and_keeps_pushing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    attempt = 0
    second_pushed = asyncio.Event()

    async def fake_stream():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield StreamEvent("update", BUNDLE_1)
            raise ClassDashConnectionError("dropped")
        yield StreamEvent("update", BUNDLE_2)
        second_pushed.set()
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    try:
        with patch(
            "custom_components.classdash.coordinator.asyncio.sleep",
            AsyncMock(),
        ):
            await coordinator.async_config_entry_first_refresh()
            # Background tasks are deliberately excluded from
            # hass.async_block_till_done(), so wait on the reconnect
            # directly instead.
            await asyncio.wait_for(second_pushed.wait(), timeout=2)

        assert attempt == 2
        assert coordinator.data.status["dueSoon"] == 2
        assert coordinator.last_update_success is True
    finally:
        coordinator._listen_task.cancel()


async def test_auth_error_after_first_update_triggers_reauth(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    async def fake_stream():
        yield StreamEvent("update", BUNDLE_1)
        raise ClassDashAuthError("token rolled")

    reauth_started = asyncio.Event()
    coordinator = _make_coordinator(hass, entry, fake_stream)
    with patch.object(
        entry, "async_start_reauth", side_effect=lambda *a, **k: reauth_started.set()
    ) as mock_reauth:
        await coordinator.async_config_entry_first_refresh()
        await asyncio.wait_for(reauth_started.wait(), timeout=2)
        mock_reauth.assert_called_once_with(hass)


async def test_auth_error_on_first_connect_fails_setup(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    async def fake_stream():
        raise ClassDashAuthError("bad token")
        yield  # pragma: no cover - makes this an async generator

    coordinator = _make_coordinator(hass, entry, fake_stream)
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator.async_config_entry_first_refresh()


async def test_first_connect_timeout_cancels_listener_task(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A first connection that never produces anything shouldn't leave the
    listener running forever — nothing else would ever stop it, since a
    config entry that fails first refresh is never unloaded."""

    async def hangs_forever():
        await asyncio.Event().wait()
        yield  # pragma: no cover - unreachable, keeps this an async generator

    coordinator = _make_coordinator(hass, entry, hangs_forever)
    with patch(
        "custom_components.classdash.coordinator.STREAM_FIRST_CONNECT_TIMEOUT", 0.05
    ):
        # UpdateFailed inside _async_update_data surfaces here as
        # ConfigEntryNotReady — async_config_entry_first_refresh's contract,
        # not something this integration controls.
        with pytest.raises(ConfigEntryNotReady):
            await coordinator.async_config_entry_first_refresh()

    await hass.async_block_till_done()
    assert coordinator._listen_task.cancelled()
