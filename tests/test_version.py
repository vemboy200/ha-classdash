"""Tests for ClassDash's version: the main device's software version, and
the repair issue for a ClassDash too old for this integration."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.classdash.api import StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras, default_update_status

STATUS = {
    "collectedAt": "2026-08-30T12:00:00.000Z",
    "minutesAgo": 1,
    "classes": 1,
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
PHYSICS = {"name": "Physics", "dueSoon": 0, "ahead": 0, "overdue": 0, "status": "known", "teacher": None, "classes": ["Physics"], "platform": "Canvas"}


def _bundle(version: str | None = "2.5.0", platform: bool = True) -> dict:
    physics = dict(PHYSICS)
    if not platform:
        del physics["platform"]
    return {
        **bundle_extras(),
        "update-status": {**default_update_status(), "currentVersion": version},
        "status": STATUS,
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [physics],
    }


async def _wait_for(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not check():
            await asyncio.sleep(0)


def _state(hass: HomeAssistant) -> str:
    return hass.states.get("sensor.classdash_due_soon").state


@pytest.fixture
def entry(hass: HomeAssistant, sample_certificate) -> MockConfigEntry:
    e = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={"host": "192.168.1.50", "port": 8734, "token": "a" * 64, CONF_CERT_PEM: sample_certificate.pem},
    )
    e.add_to_hass(hass)
    return e


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"classdash_too_old_{entry.entry_id}")


_real_sleep = asyncio.sleep


async def _no_wait(_delay: float) -> None:
    await _real_sleep(0)


async def test_software_version_on_the_main_device(hass: HomeAssistant, entry) -> None:
    step = asyncio.Event()

    async def stream():
        yield StreamEvent("update", _bundle("2.5.0"))
        await step.wait()
        yield StreamEvent("update", _bundle("2.5.1"))
        await asyncio.Event().wait()

    with patch("custom_components.classdash.ClassDashClient", autospec=True) as client_cls:
        client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)

        def main() -> dr.DeviceEntry:
            return dev_reg.async_get_device_by_identifier((DOMAIN, entry.unique_id), entry.entry_id)

        await _wait_for(lambda: main().sw_version == "2.5.0")
        step.set()
        await _wait_for(lambda: main().sw_version == "2.5.1")
        assert _issue(hass, entry) is None

        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize(
    ("old", "translation_key", "placeholders"),
    [
        # An older release.
        (_bundle("2.3.9"), "classdash_too_old", {"version": "2.3.9"}),
        # No version to go by (never checked for updates), and no class
        # platform: ClassDash v2.3.0 or older.
        (_bundle(None, platform=False), "classdash_too_old_unknown", {}),
    ],
)
async def test_too_old_classdash_raises_an_issue_until_updated(
    hass: HomeAssistant, entry, old: dict, translation_key: str, placeholders: dict
) -> None:
    connections = 0
    updated = asyncio.Event()

    async def stream():
        nonlocal connections
        connections += 1
        if not updated.is_set():
            yield StreamEvent("update", old)
        else:
            yield StreamEvent("update", _bundle("2.5.0"))
        await asyncio.Event().wait()

    with (
        patch("custom_components.classdash.ClassDashClient", autospec=True) as client_cls,
        patch("custom_components.classdash.coordinator.asyncio.sleep", _no_wait),
    ):
        client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        await _wait_for(lambda: _issue(hass, entry) is not None)
        issue = _issue(hass, entry)
        assert issue.translation_key == translation_key
        assert issue.translation_placeholders == {
            "minimum": "2.4.0",
            "url": "https://github.com/vemboy200/ClassDash/releases",
            **placeholders,
        }
        assert issue.severity is ir.IssueSeverity.ERROR
        assert _state(hass) == "unavailable"

        # Updating ClassDash fixes it on the next reconnect, no reload.
        updated.set()
        await _wait_for(lambda: _issue(hass, entry) is None)
        await _wait_for(lambda: _state(hass) == "0")

        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("version", ["2.3.0-11-gc833fbb", "2.4.0-dev", "dev"])
async def test_dev_builds_are_never_too_old(hass: HomeAssistant, entry, version: str) -> None:
    async def stream():
        yield StreamEvent("update", _bundle(version))
        await asyncio.Event().wait()

    with patch("custom_components.classdash.ClassDashClient", autospec=True) as client_cls:
        client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await _wait_for(lambda: _state(hass) == "0")

        assert _issue(hass, entry) is None
        main = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, entry.unique_id), entry.entry_id)
        assert main.sw_version == version

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_no_version_but_new_enough_data_still_works(hass: HomeAssistant, entry) -> None:
    """A ClassDash that hasn't checked for updates yet has no version, but
    sends everything a new enough one does."""
    async def stream():
        yield StreamEvent("update", _bundle(None))
        await asyncio.Event().wait()

    with patch("custom_components.classdash.ClassDashClient", autospec=True) as client_cls:
        client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await _wait_for(lambda: _state(hass) == "0")
        assert _issue(hass, entry) is None

        assert await hass.config_entries.async_unload(entry.entry_id)


async def test_removing_the_entry_removes_its_issue(hass: HomeAssistant, entry) -> None:
    async def stream():
        yield StreamEvent("update", _bundle("2.3.9"))
        await asyncio.Event().wait()

    with (
        patch("custom_components.classdash.ClassDashClient", autospec=True) as client_cls,
        patch("custom_components.classdash.coordinator.asyncio.sleep", _no_wait),
    ):
        client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await _wait_for(lambda: _issue(hass, entry) is not None)

        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
    assert _issue(hass, entry) is None


async def test_a_dev_build_missing_data_is_not_called_too_old(
    hass: HomeAssistant, entry, caplog
) -> None:
    """Dev builds are never flagged: missing data from one is a bug, not
    an old version, and only gets logged."""
    async def stream():
        yield StreamEvent("update", _bundle("2.3.0-11-gc833fbb", platform=False))
        await asyncio.Event().wait()

    with (
        patch("custom_components.classdash.ClassDashClient", autospec=True) as client_cls,
        patch("custom_components.classdash.coordinator.asyncio.sleep", _no_wait),
    ):
        client_cls.return_value.async_stream_updates = stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        await _wait_for(lambda: "unexpected error" in caplog.text)
        assert _issue(hass, entry) is None

        assert await hass.config_entries.async_unload(entry.entry_id)
