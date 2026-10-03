"""Tests for the Checking binary sensor and the Check progress sensor."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.classdash.api import StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

STATUS = {
    "collectedAt": "2026-08-30T12:00:00.000Z",
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
    "collecting": False,
    "schoolToday": None,
    "scheduleToday": None,
}


def _bundle(collecting: bool, done=None, total=None, percent=None) -> dict:
    return {
        **bundle_extras(),
        "status": {**STATUS, "collecting": collecting},
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [],
        "collection": {
            "running": collecting,
            "done": done,
            "total": total,
            "percent": percent,
            "updatedAt": "2026-08-30T12:00:00.000Z",
        },
    }


def _entry(hass: HomeAssistant, sample_certificate) -> MockConfigEntry:
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
    return entry


async def _wait_for(check: Callable[[], bool], timeout: float = 2) -> None:
    async with asyncio.timeout(timeout):
        while not check():
            await asyncio.sleep(0)


async def test_check_progress_is_disabled_by_default(
    hass: HomeAssistant, sample_certificate
) -> None:
    async def stream():
        yield StreamEvent("update", _bundle(False))
        await asyncio.Event().wait()

    entry = _entry(hass, sample_certificate)
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry_entry = er.async_get(hass).async_get("sensor.classdash_check_progress")
        assert registry_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert hass.states.get("sensor.classdash_check_progress") is None
        # Checking stays on: it's the one an automation would use.
        assert hass.states.get("binary_sensor.classdash_checking") is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_checking_and_progress_follow_a_check(
    hass: HomeAssistant, sample_certificate
) -> None:
    """Idle, then a check partway through, then the heartbeat saying it's
    no longer running (a check that died sends no last word, so the
    heartbeat's status is what clears it)."""
    step = asyncio.Event()

    async def stream():
        yield StreamEvent("update", _bundle(False))
        await step.wait()
        step.clear()
        yield StreamEvent("update", _bundle(True, done=2, total=5, percent=40))
        await step.wait()
        yield StreamEvent("heartbeat", {**STATUS, "collecting": False})
        await asyncio.Event().wait()

    entry = _entry(hass, sample_certificate)
    # Check progress is disabled by default; registering it ahead of setup
    # as enabled is the same as a user having turned it on.
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        "192.168.1.50:8734_check_progress",
        suggested_object_id="classdash_check_progress",
        config_entry=entry,
    )

    def state(entity_id: str) -> str | None:
        s = hass.states.get(entity_id)
        return s.state if s else None

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await _wait_for(lambda: state("binary_sensor.classdash_checking") == "off")
        assert state("sensor.classdash_check_progress") == "unknown"

        step.set()
        await _wait_for(lambda: state("binary_sensor.classdash_checking") == "on")
        progress = hass.states.get("sensor.classdash_check_progress")
        assert progress.state == "40"
        assert progress.attributes["unit_of_measurement"] == "%"
        assert progress.attributes["done"] == 2
        assert progress.attributes["total"] == 5

        step.set()
        await _wait_for(lambda: state("binary_sensor.classdash_checking") == "off")
        progress = hass.states.get("sensor.classdash_check_progress")
        assert progress.state == "unknown"
        assert progress.attributes["done"] is None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
