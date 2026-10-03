"""Tests for the push-based coordinator's stream handling.

Covers the parts test_init.py's end-to-end setup test doesn't reach:
starting out unavailable until the first push (setup doesn't wait for
ClassDash), later pushes updating coordinator.data without a poll,
reconnecting after a dropped connection, reauth triggering from inside
the running stream, and what a manual refresh does with no poll to run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.coordinator import ClassDashCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

BUNDLE_1 = {
    **bundle_extras(),
    "status": {"collecting": False, "schoolToday": None, "scheduleToday": None, "dueSoon": 1, "overdue": 0, "ahead": 0, "done": 0, "announcements": 0, "collectedAt": "2026-08-30T12:00:00.000Z", "minutesAgo": 1, "classes": 0, "total": 0, "removed": 0, "language": "en"},
    "due-soon": [{"title": "first"}],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [],
}
BUNDLE_2 = {
    **bundle_extras(),
    "status": {"collecting": False, "schoolToday": None, "scheduleToday": None, "dueSoon": 2, "overdue": 0, "ahead": 0, "done": 0, "announcements": 0, "collectedAt": "2026-08-30T12:00:00.000Z", "minutesAgo": 1, "classes": 0, "total": 0, "removed": 0, "language": "en"},
    "due-soon": [{"title": "first"}, {"title": "second"}],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [],
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
    e.mock_state(hass, ConfigEntryState.LOADED)
    return e


def _make_coordinator(hass: HomeAssistant, entry: MockConfigEntry, stream_fn):
    client = AsyncMock()
    client.async_stream_updates = stream_fn
    return ClassDashCoordinator(hass, entry, client, "main-device-id")


# The real one, kept before any test patches asyncio.sleep (which is the
# same function everywhere, not just inside coordinator.py).
_real_sleep = asyncio.sleep


async def _no_wait(_delay: float) -> None:
    """Skip reconnect backoff, but still yield, so a ClassDash that never
    answers can't starve the event loop."""
    await _real_sleep(0)


@asynccontextmanager
async def _running(coordinator: ClassDashCoordinator) -> AsyncIterator[None]:
    """Run the listener for the duration of a test, without the real
    reconnect waits, and stop it afterwards (normally the config entry's
    unload does that)."""
    with patch("custom_components.classdash.coordinator.asyncio.sleep", _no_wait):
        task = coordinator.async_start()
        try:
            yield
        finally:
            task.cancel()


async def _wait_for(check: Callable[[], bool], timeout: float = 2) -> None:
    # Background tasks are deliberately excluded from
    # hass.async_block_till_done(), so wait on the condition directly.
    async with asyncio.timeout(timeout):
        while not check():
            await _real_sleep(0)


async def test_unavailable_until_the_first_push(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Setup doesn't wait for ClassDash, so before the first push there's
    no data and entities read as unavailable — then the push fills it in."""
    release = asyncio.Event()

    async def fake_stream():
        await release.wait()
        yield StreamEvent("update", BUNDLE_1)
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    assert coordinator.data is None
    assert coordinator.last_update_success is False

    async with _running(coordinator):
        release.set()
        await _wait_for(lambda: coordinator.data is not None)

        assert coordinator.data.status["dueSoon"] == 1
        assert coordinator.last_update_success is True


async def test_subsequent_push_updates_coordinator_data(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A second event reaches coordinator.data with no polling involved —
    this is the whole point of local_push."""
    release_second = asyncio.Event()

    async def fake_stream():
        yield StreamEvent("update", BUNDLE_1)
        await release_second.wait()
        yield StreamEvent("update", BUNDLE_2)
        await asyncio.Event().wait()  # keep the "connection" open

    coordinator = _make_coordinator(hass, entry, fake_stream)
    async with _running(coordinator):
        await _wait_for(lambda: coordinator.data is not None)
        assert coordinator.data.status["dueSoon"] == 1

        release_second.set()
        await _wait_for(lambda: coordinator.data.status["dueSoon"] == 2)
        assert coordinator.last_update_success is True


async def test_heartbeat_refreshes_status_without_touching_lists(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A heartbeat carries only /api/status — it should refresh that (so
    minutes_ago actually ticks over time) without replacing the
    assignment/announcement lists, which it doesn't even carry."""
    heartbeat_status = {**BUNDLE_1["status"], "minutesAgo": 3}

    async def fake_stream():
        yield StreamEvent("update", BUNDLE_1)
        yield StreamEvent("heartbeat", heartbeat_status)
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    async with _running(coordinator):
        await _wait_for(
            lambda: coordinator.data is not None
            and coordinator.data.status == heartbeat_status
        )
        # Untouched — a heartbeat carries no list data at all.
        assert coordinator.data.due_soon == BUNDLE_1["due-soon"]
        assert coordinator.last_update_success is True


async def test_heartbeat_before_any_update_is_ignored(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Shouldn't happen in practice (ClassDash always sends "update"
    immediately on connect, well under the 60s heartbeat interval), but
    a heartbeat with no data yet must not crash or count as connected."""
    release_update = asyncio.Event()
    heartbeat_seen = asyncio.Event()

    async def fake_stream():
        yield StreamEvent("heartbeat", {"dueSoon": 0, "collectedAt": "2026-08-30T12:00:00.000Z", "minutesAgo": 1, "classes": 0, "total": 0, "overdue": 0, "ahead": 0, "done": 0, "announcements": 0, "removed": 0, "language": "en", "collecting": False, "schoolToday": None, "scheduleToday": None})
        heartbeat_seen.set()
        await release_update.wait()
        yield StreamEvent("update", BUNDLE_1)
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    async with _running(coordinator):
        await asyncio.wait_for(heartbeat_seen.wait(), timeout=2)
        assert coordinator.data is None
        assert coordinator.last_update_success is False

        release_update.set()
        await _wait_for(lambda: coordinator.data is not None)
        assert coordinator.data.status["dueSoon"] == 1


async def test_reconnects_after_connection_error_and_keeps_pushing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    attempt = 0

    async def fake_stream():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield StreamEvent("update", BUNDLE_1)
            raise ClassDashConnectionError("dropped")
        yield StreamEvent("update", BUNDLE_2)
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    async with _running(coordinator):
        await _wait_for(
            lambda: coordinator.data is not None
            and coordinator.data.status["dueSoon"] == 2
        )

    assert attempt == 2
    assert coordinator.last_update_success is True


async def test_unexpected_error_reconnects_instead_of_killing_the_listener(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A malformed event (or any other bug in event processing) must not
    silently end the listener task forever — caught live while adding
    the classes roster: an incomplete fake bundle raised a bare KeyError
    that neither ClassDashAuthError nor ClassDashConnectionError caught,
    and without a catch-all, that exception propagated straight out of
    _listen(), ending the task with no reconnect ever happening again."""
    attempt = 0
    malformed_bundle = {
        "status": BUNDLE_1["status"],
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        # "classes" missing on purpose — the same shape of bug that
        # motivated this test, triggering a bare KeyError while parsing.
    }

    async def fake_stream():
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            yield StreamEvent("update", BUNDLE_1)
            yield StreamEvent("update", malformed_bundle)  # then breaks
            return
        yield StreamEvent("update", BUNDLE_2)
        await asyncio.Event().wait()

    coordinator = _make_coordinator(hass, entry, fake_stream)
    async with _running(coordinator):
        await _wait_for(
            lambda: coordinator.data is not None
            and coordinator.data.status["dueSoon"] == 2
        )

    assert attempt == 2
    assert coordinator.last_update_success is True


@pytest.mark.parametrize("push_first", [False, True])
async def test_auth_error_triggers_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, push_first: bool
) -> None:
    """A rejected token starts reauth whether it happens on the very first
    connection or after the token is rolled later on — and stops
    retrying, since the reauth reload replaces this coordinator."""
    attempts = 0

    async def fake_stream():
        nonlocal attempts
        attempts += 1
        if push_first:
            yield StreamEvent("update", BUNDLE_1)
        raise ClassDashAuthError("token rolled")

    reauth_started = asyncio.Event()
    coordinator = _make_coordinator(hass, entry, fake_stream)
    with patch.object(
        entry, "async_start_reauth", side_effect=lambda *a, **k: reauth_started.set()
    ) as mock_reauth:
        async with _running(coordinator):
            await asyncio.wait_for(reauth_started.wait(), timeout=2)
            mock_reauth.assert_called_once_with(hass)

    assert attempts == 1


async def test_unreachable_at_setup_logs_once_then_connects(
    hass: HomeAssistant, entry: MockConfigEntry, caplog: pytest.LogCaptureFixture
) -> None:
    """ClassDash being off when Home Assistant starts is normal (a laptop
    that's shut down): log it once, keep retrying quietly, and say so
    when it finally connects."""
    attempt = 0

    async def fake_stream():
        nonlocal attempt
        attempt += 1
        if attempt <= 3:
            raise ClassDashConnectionError("connection refused")
        yield StreamEvent("update", BUNDLE_1)
        await asyncio.Event().wait()

    caplog.set_level(logging.INFO, logger="custom_components.classdash")
    coordinator = _make_coordinator(hass, entry, fake_stream)
    async with _running(coordinator):
        await _wait_for(lambda: coordinator.data is not None)

    assert attempt == 4
    assert caplog.text.count("isn't reachable yet") == 1
    assert "Reconnected to ClassDash at 192.168.1.50:8734" in caplog.text
    assert coordinator.last_update_success is True


async def test_manual_refresh_only_succeeds_while_connected(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """There's nothing to poll, so a manual refresh (update_entity) hands
    back the last push while connected, and fails while not — rather
    than making stale data look current again."""
    drop = asyncio.Event()
    dropped = asyncio.Event()

    async def fake_stream():
        if drop.is_set():
            dropped.set()
            raise ClassDashConnectionError("gone")
        yield StreamEvent("update", BUNDLE_1)
        await drop.wait()
        raise ClassDashConnectionError("gone")

    coordinator = _make_coordinator(hass, entry, fake_stream)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    async with _running(coordinator):
        await _wait_for(lambda: coordinator.data is not None)
        assert await coordinator._async_update_data() is coordinator.data

        drop.set()
        await asyncio.wait_for(dropped.wait(), timeout=2)
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
