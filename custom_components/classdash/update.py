"""Update entity for ClassDash — the macOS app's own self-update check.

This tracks ClassDash's app version against GitHub, not this integration's
own version — see CONTRIBUTING.md's "Update check" section and
16-summary.swift's checkForUpdates() for the actual mechanism this reads.

Deliberately read-only, not just download-only like it used to be. Two
independent reasons neither of which a code fix here can close: (1) Home
Assistant's own generic update card has no UI for a "downloading" phase
distinct from "installing" — confirmed directly against
home-assistant/frontend — so an active install button always just shows
"Installing..." for the whole download regardless of what this entity's
own status actually says. (2) computeStatus() in 26-update-check.js
(what /api/update-status reports) never checks whether a downloaded
.dmg has actually gone missing since being marked ready — it can keep
reporting "ready to install" long after the file's gone (deleted, moved,
evicted by a cloud-synced folder), the same staleness bug
16-summary.swift's own native "Check for Updates…" menu item just got
fixed for (commit 3952d82), but only on that side. Between the two, an
interactive install here was more confusing than useful. This just shows
the version comparison, the real GitHub release notes, and whatever
status/downloaded_version/ready_to_install ClassDash's own check last
found as plain informational attributes — actually downloading and
installing stays entirely on the Mac itself, via ClassDash's own update
banner or its "Check for Updates…" menu item.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import aiohttp

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

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
    """ClassDash's own installed app version vs. the latest GitHub release — read-only."""

    _attr_has_entity_name = True
    _attr_translation_key = "app_update"
    _attr_supported_features = UpdateEntityFeature.RELEASE_NOTES
    _attr_release_summary = (
        "Read-only — actually downloading and installing an update stays "
        "on the Mac itself, via ClassDash's own update banner or its "
        "\"Check for Updates…\" menu item."
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
