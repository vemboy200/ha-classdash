"""Push-based data coordinator for ClassDash.

ClassDash's `/api/stream` was built specifically so this integration
wouldn't have to poll: it sends the full current snapshot immediately on
connect, then again only when a collection pass actually changes
something. So instead of a fixed `update_interval`, a single long-lived
background task holds that connection open for the lifetime of the config
entry and pushes each snapshot straight into the coordinator.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ClassDashAuthError, ClassDashClient, ClassDashConnectionError
from .const import (
    DOMAIN,
    STREAM_RECONNECT_MAX_SECONDS,
    STREAM_RECONNECT_MIN_SECONDS,
    STREAM_UNAVAILABLE_THRESHOLD_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class ClassDashData:
    """One /api/stream snapshot, narrowed to what the sensors use."""

    status: dict[str, Any]
    due_soon: list[dict[str, Any]]
    ahead: list[dict[str, Any]]
    overdue: list[dict[str, Any]]
    done: list[dict[str, Any]]
    announcements: list[dict[str, Any]]
    # {"name", "dueSoon", "ahead", "overdue"} per class — the merged
    # cross-platform roster from /api/classes. Empty-count classes only
    # appear here at all if ClassDash's own `showEmptyClasses` setting is
    # on (off by default); see class_names' docstring for why this alone
    # still isn't a complete source of class names on its own.
    classes: list[dict[str, Any]]
    # {"classroom": {...}, "canvas": {...}, "edpuzzle": {...}}, each
    # {"status": "ok"|"problem"|"unknown", "at": iso|None, "detail": str|None}
    # — pipeline health, not "what's due"; see /api/check-status.
    check_status: dict[str, dict[str, Any]]
    # Reminders the user typed in themselves, not read from any platform
    # — /api/virtual. Optionally assigned to a class (`class` may be
    # None), mixed together regardless of state (overdue/upcoming/
    # undated/done all in one list, unlike real assignments which each
    # have their own bucket/handle) — is_done()/is_hidden() sort that out.
    virtual: list[dict[str, Any]]
    # ClassDash's own macOS-app update check — /api/update-status.
    # {"currentVersion", "latestVersion", "url", "checkedAt",
    # "updateAvailable", "dismissedVersion"}, plus "downloading",
    # "readyToInstall", "readyVersion", "downloadedPath" once a download
    # has actually been attempted. All fields are None/False until the
    # app's own check has run at least once. Unlike everything else in
    # this snapshot, this can go stale between real pushes: writing
    # update-status.json doesn't trigger a broadcast on its own (only the
    # state/stream files are watched) — update.py is purely read-only, so
    # this just lags behind an in-progress check/download until the next
    # real collection pass happens to broadcast, rather than refetching
    # it directly the way an earlier, interactive version of that entity
    # used to.
    update_status: dict[str, Any]


type ClassDashConfigEntry = ConfigEntry[ClassDashCoordinator]


def class_names(data: ClassDashData) -> set[str]:
    """Every distinct class name known from one snapshot, minus orphaned ones.

    /api/classes now covers Classroom+Canvas+Edpuzzle (it didn't when this
    function was first written — that gap is why due/ahead/overdue/
    announcements were scanned directly instead, and why that scan still
    happens: /api/classes' own roster only includes a class with nothing
    currently due when ClassDash's `showEmptyClasses` setting is on, and
    never includes an announcement-only class at all (its "present" set
    is built strictly from due-soon/ahead/overdue, not announcements) — so
    the union of both sources is still the real complete picture, not
    either alone.

    Each roster entry also carries `status`: "known" (the platform still
    lists the class) or "orphaned" (a real transfer, or a class hidden on
    Classroom's own side — old data for it is still around, but it's not
    a real live class any more). An orphaned class is excluded here even
    if it still has lingering items in the due/overdue lists — the whole
    point of the roster carrying `status` at all is so a client doesn't
    have to keep an entity around for a class that's genuinely gone.
    ClassDash's CONTRIBUTING.md is explicit that this exact ambiguity
    once flooded a Home Assistant integration (this one) with an entity
    for a class the student had already moved on from.
    """
    orphaned = {c["name"] for c in data.classes if c.get("status") == "orphaned"}
    from_roster = {c["name"] for c in data.classes if c.get("status") != "orphaned"}
    from_items = {
        name
        for item in (
            *data.due_soon,
            *data.ahead,
            *data.overdue,
            *data.done,
            *data.announcements,
            *data.virtual,
        )
        if (name := item.get("class"))
    }
    return (from_roster | from_items) - orphaned


def is_hidden(item: dict[str, Any]) -> bool:
    """A hidden item was dismissed on purpose. /api/overdue and friends no
    longer filter these out server-side (everything goes out, tagged, per
    CONTRIBUTING.md) — a client filters if it wants the old "actionable
    only" behavior, same as /api/status's own counts already do."""
    return "hidden" in item.get("tags", [])


def is_done(item: dict[str, Any]) -> bool:
    """Turned in / marked done. Used to keep completed virtual reminders
    off a class's calendar — /api/virtual mixes every state into one
    list, unlike real assignments where "done" is already its own
    separate bucket that due/ahead/overdue structurally can't contain."""
    return "done" in item.get("tags", [])


# Mirrors 24-virtual-assignments.js's own "due within 7 days" cutoff for
# real assignments (`dueAt - now <= 7 * 864e5`, ClassDash's own
# due-soon/ahead split). /api/virtual doesn't expose which bucket a
# reminder landed in server-side the way real assignments do (each real
# bucket is its own REST endpoint) — it hands back one flat list with
# just a due date and done/hidden tags — so sensor.py has to re-derive
# due-soon/ahead/overdue/done locally from that. If ClassDash's own
# cutoff ever changes, this needs updating to match; there's no way for
# this integration to read it live.
VIRTUAL_DUE_SOON_WINDOW = timedelta(days=7)


def virtual_bucket(item: dict[str, Any], now: datetime) -> str | None:
    """Which of "due_soon"/"ahead"/"overdue"/"done" a virtual reminder
    falls into, for the sensors that count real assignments the same
    way — matches ClassDashData's own field names so callers can use it
    directly with getattr(). None for a hidden item, or an undated one
    that isn't done either: a real assignment never ends up in
    due_soon/ahead/overdue without a due date (see 17-api.js's
    treatUndatedAsUrgent handling — an undated one is either resolved to
    "tomorrow" before it ever reaches these buckets, or excluded
    entirely), so a virtual reminder shouldn't either. Being done doesn't
    need a due date at all, unlike the other three."""
    if is_hidden(item):
        return None
    if is_done(item):
        return "done"
    due = dt_util.parse_datetime(item["due"]) if item.get("due") else None
    if due is None:
        return None
    if due < now:
        return "overdue"
    if due - now <= VIRTUAL_DUE_SOON_WINDOW:
        return "due_soon"
    return "ahead"


def _parse_snapshot(bundle: dict[str, Any]) -> ClassDashData:
    """The stream's top-level keys are each REST handle's path with the
    leading /api/ stripped — so the list endpoints keep their hyphens
    ("due-soon"), unlike the camelCase keys inside `status` itself."""
    return ClassDashData(
        status=bundle["status"],
        due_soon=bundle["due-soon"],
        ahead=bundle["ahead"],
        overdue=bundle["overdue"],
        done=bundle["done"],
        announcements=bundle["announcements"],
        classes=bundle["classes"],
        check_status=bundle["check-status"],
        virtual=bundle["virtual"],
        update_status=bundle["update-status"],
    )


class ClassDashCoordinator(DataUpdateCoordinator[ClassDashData]):
    """Holds the /api/stream connection open and pushes each update."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ClassDashConfigEntry,
        client: ClassDashClient,
        main_device_id: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            # No update_interval: this coordinator is push-driven, by the
            # listener async_start launches.
            update_interval=None,
        )
        self.client = client
        # The main "ClassDash" device's registry id, which every class
        # device points at with via_device_id. Registered up front in
        # async_setup_entry, so it exists before any class device does.
        self.main_device_id = main_device_id
        # Setup doesn't wait for ClassDash (it's often on a laptop that's
        # asleep, shut down, or off this network), so there's no data
        # until the first push. Starting out "failed" keeps every entity
        # unavailable until then, the same as an outage later on.
        self.last_update_success = False
        self._connected = False
        self._logged_unreachable = False
        # Which classes sensor.py/calendar.py have each already added
        # entities for, *this process*. Deliberately not derived from the
        # entity/device registry — those persist across a restart on
        # disk, but the actual Entity objects don't; checking the
        # registry for "already exists" would wrongly skip re-adding
        # every class on every restart, leaving them stuck showing
        # unavailable (a real bug this replaced — a fresh coordinator
        # means these start empty every time, so a fresh async_setup_entry
        # always re-adds everything it currently knows about). Two
        # separate sets, not one shared: sensor.py and calendar.py add
        # different entities for the same class and each needs its own
        # "have I added this one yet" answer, not each other's.
        self.known_class_sensors: set[str] = set()
        self.known_class_calendars: set[str] = set()

    @callback
    def async_start(self) -> asyncio.Task[None]:
        """Start the /api/stream listener. It runs until the config entry
        unloads, which cancels it."""
        return self.config_entry.async_create_background_task(
            self.hass, self._listen(), name=f"{DOMAIN}_stream"
        )

    async def _async_update_data(self) -> ClassDashData:
        """Only reached by a manual refresh (homeassistant.update_entity).

        Data arrives by push, so there's nothing to fetch: hand back what
        the stream last sent while it's connected, and fail while it
        isn't, so a manual refresh can't make stale data look current.
        """
        if not self._connected or self.data is None:
            raise UpdateFailed("not connected to ClassDash")
        return self.data

    def _push(self, data: ClassDashData) -> None:
        """Hand a pushed snapshot to the entities, logging a recovery.

        DataUpdateCoordinator logs going unavailable on its own, but
        async_set_updated_data says nothing on the way back — this is
        the matching "it's back" line, so a log shows whether the
        connection ever recovered without a reload.
        """
        if not self.last_update_success and (
            self.data is not None or self._logged_unreachable
        ):
            _LOGGER.info(
                "Reconnected to ClassDash at %s:%s",
                self.config_entry.data[CONF_HOST],
                self.config_entry.data[CONF_PORT],
            )
        self.async_set_updated_data(data)

    def _connection_lost(self, err: Exception, backoff: float) -> None:
        """Mark the entities unavailable once retries have gone on long
        enough — a single missed reconnect shouldn't flash them."""
        self._connected = False
        if self.data is None:
            # Never connected since setup: entities are already
            # unavailable, and DataUpdateCoordinator only logs the
            # available→unavailable change, so say so here, once.
            if not self._logged_unreachable:
                _LOGGER.info(
                    "ClassDash at %s:%s isn't reachable yet, will keep trying: %s",
                    self.config_entry.data[CONF_HOST],
                    self.config_entry.data[CONF_PORT],
                    err,
                )
                self._logged_unreachable = True
            return
        if backoff >= STREAM_UNAVAILABLE_THRESHOLD_SECONDS:
            self.async_set_update_error(err)

    async def _listen(self) -> None:
        """Reconnect forever, with backoff, until the config entry unloads
        (which cancels this task automatically)."""
        backoff = STREAM_RECONNECT_MIN_SECONDS
        while True:
            try:
                async for event in self.client.async_stream_updates():
                    backoff = STREAM_RECONNECT_MIN_SECONDS
                    if event.event == "update":
                        self._connected = True
                        self._push(_parse_snapshot(event.data))
                    elif event.event == "heartbeat" and self.data is not None:
                        # A freshness signal, not new assignment/announcement
                        # data (CONTRIBUTING.md is explicit about that) — swap
                        # in just the refreshed status (collectedAt/minutesAgo
                        # tick even when nothing else has), keep the existing
                        # lists untouched. Ignored before the first "update",
                        # which ClassDash always sends first on connect anyway.
                        self._push(dataclasses.replace(self.data, status=event.data))
                # The stream ended without an error (server closed it
                # cleanly) — treat the same as a connection error below:
                # reconnect after a short wait.
                raise ClassDashConnectionError("stream closed")
            except ClassDashAuthError:
                # The token was rolled, at setup or later. Reauth reloads
                # the entry, which replaces this coordinator (and cancels
                # this task) entirely — nothing left to do here.
                self._connected = False
                self.config_entry.async_start_reauth(self.hass)
                return
            except ClassDashConnectionError as err:
                _LOGGER.debug(
                    "classdash stream disconnected, retrying in %ss: %s",
                    backoff,
                    err,
                )
                self._connection_lost(err, backoff)
            except Exception as err:  # noqa: BLE001
                # Anything else — a malformed/unexpected event shape, a bug
                # in this code's own parsing — must not be allowed to
                # terminate this task silently. Without a catch-all here,
                # an exception raised while processing one event inside the
                # `async for` above would propagate straight out of
                # _listen() itself: no reconnect would ever happen again
                # for the rest of the config entry's lifetime, and nothing
                # would visibly say why. Logged at ERROR (not the debug
                # level ClassDashConnectionError gets) since this
                # represents a real bug or an unexpected server change,
                # not an ordinary network hiccup.
                _LOGGER.exception(
                    "classdash stream: unexpected error, retrying in %ss", backoff
                )
                self._connection_lost(err, backoff)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, STREAM_RECONNECT_MAX_SECONDS)
