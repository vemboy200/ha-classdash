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

import asyncio
import dataclasses
import re
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ClassDashAuthError, ClassDashConnectionError
from .coordinator import ClassDashConfigEntry, ClassDashCoordinator
from .devices import main_device_info

PARALLEL_UPDATES = 0

# Matches the path of exactly the release_url ClassDash's own update
# check builds — https://github.com/<owner>/<repo>/releases/tag/<tag> —
# to pull owner/repo/tag back out for GitHub's REST API, which wants
# them as separate path segments rather than a release page URL. The
# host is checked separately (not folded into this pattern) — this
# integration only ever talks to ClassDash's own home API otherwise, so
# treating update_status["url"] as trusted enough to build a request to
# whatever host it names, sight unseen, isn't a chance worth taking.
_RELEASE_URL_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/releases/tag/(.+)$")

# How long async_install actually waits and polls before giving up on
# watching the download it started — see async_install's own docstring
# for why it polls at all instead of returning right away. 3s * 200 =
# 10 minutes, generous for a release .dmg over a normal home
# connection without polling so tightly it's spamming the API.
_INSTALL_POLL_INTERVAL = 3
_INSTALL_POLL_ATTEMPTS = 200


def _parse_github_release_url(url: str) -> tuple[str, str, str] | None:
    parsed = urlparse(url)
    if parsed.netloc != "github.com":
        return None
    match = _RELEASE_URL_PATH_RE.match(parsed.path)
    return match.groups() if match else None  # type: ignore[return-value]


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
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.PROGRESS
        | UpdateEntityFeature.RELEASE_NOTES
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
        return self._status.get("status") == "downloading"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """status is ClassDash's own single-word summary (unknown/error/
        downloading/ready/available/up_to_date, computed fresh on every
        read — see 26-update-check.js's computeStatus()) — surfaced
        as-is since it's already the authoritative answer to "what's it
        doing right now", not something worth re-deriving here.
        downloaded_version is the version actually sitting downloaded,
        distinct from installed_version (running) and latest_version
        (GitHub's newest) — null until something's been downloaded."""
        return {
            "status": self._status.get("status"),
            "downloaded_version": self._status.get("downloadedVersion"),
            "ready_to_install": self._status.get("readyToInstall", False),
        }

    async def async_install(
        self, version: str | None, backup: bool, **kwargs: Any
    ) -> None:
        """Starts the download, then waits out the whole thing.

        This has to actually wait, not just kick the download off and
        return — ClassDash doesn't push update-status.json changes over
        /api/stream (see ClassDashData.update_status's own docstring),
        so nothing would ever tell this entity the download finished.
        Returning early left in_progress stuck true until some unrelated
        event happened to refresh the coordinator, which in practice
        meant reloading the integration by hand — the actual bug this
        polling loop exists to fix. Each poll's refresh also pushes a
        real state update along the way (via
        coordinator.async_set_updated_data), so the UI reflects the
        download settling, not just its start and end.
        """
        try:
            await self.coordinator.client.async_download_update()
        except ClassDashAuthError as err:
            raise HomeAssistantError("ClassDash rejected the bearer token") from err
        except ClassDashConnectionError as err:
            raise HomeAssistantError(
                f"Could not reach ClassDash's home API: {err}"
            ) from err

        for _ in range(_INSTALL_POLL_ATTEMPTS):
            await asyncio.sleep(_INSTALL_POLL_INTERVAL)
            if not await self._async_refresh_status():
                continue
            if not self.in_progress:
                break
        else:
            # Ran out of polling attempts without downloading ever
            # settling — leave it showing "in progress" rather than
            # raising here, since for all this knows the download itself
            # is still healthily running (a slow connection, a large
            # release); the poll budget running out says nothing
            # concrete about whether it succeeded or failed.
            return

        if self._status.get("status") == "error":
            raise HomeAssistantError(
                "ClassDash couldn't download the update: "
                f"{self._status.get('error') or 'unknown error'}"
            )

    async def async_release_notes(self) -> str | None:
        """The actual GitHub release body (markdown) for latest_version.

        ClassDash's own /api/update-status only ever gives a version
        number and a link to the release page (release_url) — not the
        notes themselves, so this reads them straight from GitHub's REST
        API using the same owner/repo/tag release_url already names.
        Best-effort: any failure (offline, rate-limited, a release
        that's since been deleted or renamed) just means no notes show
        up in the more-info dialog — release_url is still there as a
        plain link either way, this is purely an enhancement over it.
        """
        parsed = _parse_github_release_url(self.release_url) if self.release_url else None
        if parsed is None:
            return None
        owner, repo, tag = parsed
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}",
                headers={"Accept": "application/vnd.github+json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except aiohttp.ClientError:
            return None
        return data.get("body")

    async def _async_refresh_status(self) -> bool:
        """update-status.json isn't watched by /api/stream (see
        ClassDashData.update_status's own docstring) — a write to it alone
        never triggers a push. Fetch it directly so this entity doesn't
        sit showing stale downloading/ready state until some unrelated
        collection pass happens to broadcast next. Best-effort: the
        download itself already started regardless of whether this
        follow-up fetch succeeds. Returns whether it actually got a
        fresh status, so async_install's poll loop knows a failed
        request isn't the same thing as "downloading turned false"."""
        try:
            status = await self.coordinator.client.async_get_update_status()
        except (ClassDashAuthError, ClassDashConnectionError):
            return False
        self.coordinator.async_set_updated_data(
            dataclasses.replace(self.coordinator.data, update_status=status)
        )
        return True
