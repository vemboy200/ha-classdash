"""Tests for the App update entity."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.classdash.api import (
    ClassDashAuthError,
    ClassDashConnectionError,
    StreamEvent,
)
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.update import _parse_github_release_url
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras, default_update_status

BASE_STATUS = {
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
}


def _bundle(update_status: dict) -> dict:
    return {
        **bundle_extras(),
        "status": BASE_STATUS,
        "due-soon": [],
        "ahead": [],
        "overdue": [],
        "announcements": [],
        "classes": [],
        "update-status": update_status,
    }


async def _setup_entry(hass: HomeAssistant, sample_certificate, mock_client_cls, bundle):
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

    async def _open_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    mock_client_cls.return_value.async_stream_updates = _open_stream
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_id(hass: HomeAssistant) -> str:
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "update", DOMAIN, "192.168.1.50:8734_app_update"
    )
    assert entity_id is not None
    return entity_id


async def test_reflects_available_update(
    hass: HomeAssistant, sample_certificate
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
        "status": "available",
        "downloadedVersion": None,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        state = hass.states.get(_entity_id(hass))

    assert state.state == "on"
    assert state.attributes["installed_version"] == "0.3.0"
    assert state.attributes["latest_version"] == "0.4.0"
    assert (
        state.attributes["release_url"]
        == "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0"
    )
    assert state.attributes["in_progress"] is False
    assert state.attributes["status"] == "available"
    assert state.attributes["downloaded_version"] is None


async def test_reflects_downloaded_but_not_installed(
    hass: HomeAssistant, sample_certificate
) -> None:
    """downloaded_version is distinct from both installed_version (what's
    actually running) and latest_version (what GitHub has) — a version
    can be sitting downloaded without being either of those, e.g. right
    after a download finishes but before the native install prompt is
    confirmed."""
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
        "status": "ready",
        "downloadedVersion": "0.4.0",
        "readyToInstall": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        state = hass.states.get(_entity_id(hass))

    assert state.attributes["status"] == "ready"
    assert state.attributes["downloaded_version"] == "0.4.0"
    assert state.attributes["ready_to_install"] is True
    assert state.attributes["in_progress"] is False


async def test_no_check_run_yet_is_unknown(
    hass: HomeAssistant, sample_certificate
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(
            hass, sample_certificate, mock_client_cls, _bundle(default_update_status())
        )
        state = hass.states.get(_entity_id(hass))

    assert state.state == "unknown"


async def test_up_to_date_is_off(hass: HomeAssistant, sample_certificate) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.3.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": False,
        "status": "up_to_date",
        "downloadedVersion": None,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        state = hass.states.get(_entity_id(hass))

    assert state.state == "off"


async def test_install_polls_until_download_finishes(
    hass: HomeAssistant, sample_certificate
) -> None:
    """update.install has to actually start the download (the only write
    the real API offers) and then keep polling /api/update-status until
    downloading turns false, not just refresh once — a write to
    update-status.json doesn't trigger a stream push the way a real
    collection pass does, so a single refresh would leave in_progress
    stuck true until something unrelated happened to refresh the
    coordinator (the actual bug this polling loop was added to fix)."""
    initial = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://example.com/release",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
        "status": "available",
        "downloadedVersion": None,
    }
    still_downloading = {**initial, "status": "downloading", "downloading": True}
    finished = {
        **initial,
        "status": "ready",
        "downloading": False,
        "readyToInstall": True,
        "readyVersion": "0.4.0",
        "downloadedVersion": "0.4.0",
        "downloadedPath": "/tmp/update-download.dmg",
    }

    with (
        patch(
            "custom_components.classdash.ClassDashClient", autospec=True
        ) as mock_client_cls,
        patch("custom_components.classdash.update.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client_cls.return_value.async_download_update = AsyncMock()
        mock_client_cls.return_value.async_get_update_status = AsyncMock(
            side_effect=[still_downloading, still_downloading, finished]
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(initial))
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )

        mock_client_cls.return_value.async_download_update.assert_called_once()
        assert mock_client_cls.return_value.async_get_update_status.call_count == 3
        state = hass.states.get(entity_id)
        assert state.attributes["in_progress"] is False
        assert state.attributes["ready_to_install"] is True
        assert state.attributes["status"] == "ready"
        assert state.attributes["downloaded_version"] == "0.4.0"


async def test_install_gives_up_after_poll_budget_without_raising(
    hass: HomeAssistant, sample_certificate
) -> None:
    """Running out of polling attempts while still downloading isn't
    treated as a failure — a slow connection or a big release isn't an
    error, just something this stopped watching. in_progress stays true,
    reflecting the last real status seen."""
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
        "status": "available",
        "downloadedVersion": None,
    }
    still_downloading = {**status, "status": "downloading", "downloading": True}

    with (
        patch(
            "custom_components.classdash.ClassDashClient", autospec=True
        ) as mock_client_cls,
        patch("custom_components.classdash.update.asyncio.sleep", new=AsyncMock()),
        patch("custom_components.classdash.update._INSTALL_POLL_ATTEMPTS", 3),
    ):
        mock_client_cls.return_value.async_download_update = AsyncMock()
        mock_client_cls.return_value.async_get_update_status = AsyncMock(
            return_value=still_downloading
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )

        assert mock_client_cls.return_value.async_get_update_status.call_count == 3
        state = hass.states.get(entity_id)
        assert state.attributes["in_progress"] is True


async def test_install_raises_when_download_stops_without_becoming_ready(
    hass: HomeAssistant, sample_certificate
) -> None:
    """downloading turning false doesn't always mean success — a failed
    fetch leaves it false too, with status reading "error" and a real
    message in `error` (ClassDash's own computeStatus()/error field) —
    that's the signal this raises on, surfacing the actual reason rather
    than a generic "something went wrong"."""
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
        "status": "available",
        "downloadedVersion": None,
    }
    failed = {
        **status,
        "status": "error",
        "downloading": False,
        "readyToInstall": False,
        "error": "download responded 404",
    }

    with (
        patch(
            "custom_components.classdash.ClassDashClient", autospec=True
        ) as mock_client_cls,
        patch("custom_components.classdash.update.asyncio.sleep", new=AsyncMock()),
    ):
        mock_client_cls.return_value.async_download_update = AsyncMock()
        mock_client_cls.return_value.async_get_update_status = AsyncMock(
            return_value=failed
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError, match="download responded 404"):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )


async def test_install_surfaces_connection_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_download_update = AsyncMock(
            side_effect=ClassDashConnectionError("refused")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )


async def test_install_surfaces_auth_error(
    hass: HomeAssistant, sample_certificate
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_download_update = AsyncMock(
            side_effect=ClassDashAuthError("bad token")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "update", "install", {"entity_id": entity_id}, blocking=True
            )


async def test_install_refresh_failure_does_not_raise(
    hass: HomeAssistant, sample_certificate
) -> None:
    """The download itself already started — a refetch that keeps
    failing every single poll shouldn't turn into a service-call error,
    the entity just stays on whatever it last had until the next real
    push (or a later poll that actually succeeds)."""
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": None,
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    with (
        patch(
            "custom_components.classdash.ClassDashClient", autospec=True
        ) as mock_client_cls,
        patch("custom_components.classdash.update.asyncio.sleep", new=AsyncMock()),
        patch("custom_components.classdash.update._INSTALL_POLL_ATTEMPTS", 3),
    ):
        mock_client_cls.return_value.async_download_update = AsyncMock()
        mock_client_cls.return_value.async_get_update_status = AsyncMock(
            side_effect=ClassDashConnectionError("dropped")
        )
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity_id = _entity_id(hass)

        await hass.services.async_call(
            "update", "install", {"entity_id": entity_id}, blocking=True
        )
        mock_client_cls.return_value.async_download_update.assert_called_once()
        assert mock_client_cls.return_value.async_get_update_status.call_count == 3


def _get_entity(hass: HomeAssistant, entity_id: str):
    return hass.data["entity_components"]["update"].get_entity(entity_id)


def test_parse_github_release_url_extracts_owner_repo_tag() -> None:
    assert _parse_github_release_url(
        "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0"
    ) == ("vemboy200", "ClassDash", "v0.4.0")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/vemboy200/ClassDash",
        "https://github.com/vemboy200/ClassDash/releases",
        "https://example.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "not a url at all",
    ],
)
def test_parse_github_release_url_rejects_anything_else(url: str) -> None:
    assert _parse_github_release_url(url) is None


async def test_release_notes_fetches_github_release_body(
    hass: HomeAssistant, sample_certificate, aioclient_mock
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    aioclient_mock.get(
        "https://api.github.com/repos/vemboy200/ClassDash/releases/tags/v0.4.0",
        json={"body": "## What's new\n- Fixed a bug"},
    )

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity = _get_entity(hass, _entity_id(hass))
        notes = await entity.async_release_notes()

    assert notes == "## What's new\n- Fixed a bug"
    assert aioclient_mock.call_count == 1


async def test_release_notes_none_when_no_release_url(
    hass: HomeAssistant, sample_certificate, aioclient_mock
) -> None:
    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(
            hass, sample_certificate, mock_client_cls, _bundle(default_update_status())
        )
        entity = _get_entity(hass, _entity_id(hass))
        notes = await entity.async_release_notes()

    assert notes is None
    assert aioclient_mock.call_count == 0


async def test_release_notes_none_on_404(
    hass: HomeAssistant, sample_certificate, aioclient_mock
) -> None:
    """The release could have been deleted/renamed since /api/update-status
    last checked — a 404 here shouldn't be treated as an integration
    error, just "no notes to show"."""
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    aioclient_mock.get(
        "https://api.github.com/repos/vemboy200/ClassDash/releases/tags/v0.4.0",
        status=404,
    )

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity = _get_entity(hass, _entity_id(hass))
        notes = await entity.async_release_notes()

    assert notes is None


async def test_release_notes_none_on_connection_error(
    hass: HomeAssistant, sample_certificate, aioclient_mock
) -> None:
    status = {
        "currentVersion": "0.3.0",
        "latestVersion": "0.4.0",
        "url": "https://github.com/vemboy200/ClassDash/releases/tag/v0.4.0",
        "checkedAt": "2026-09-01T00:00:00.000Z",
        "updateAvailable": True,
    }
    aioclient_mock.get(
        "https://api.github.com/repos/vemboy200/ClassDash/releases/tags/v0.4.0",
        exc=aiohttp.ClientConnectionError("refused"),
    )

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        await _setup_entry(hass, sample_certificate, mock_client_cls, _bundle(status))
        entity = _get_entity(hass, _entity_id(hass))
        notes = await entity.async_release_notes()

    assert notes is None
