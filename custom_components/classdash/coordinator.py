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
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ClassDashAuthError, ClassDashClient, ClassDashConnectionError
from .const import (
    DOMAIN,
    STREAM_FIRST_CONNECT_TIMEOUT,
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
    )


class ClassDashCoordinator(DataUpdateCoordinator[ClassDashData]):
    """Holds the /api/stream connection open and pushes each update."""

    def __init__(
        self, hass: HomeAssistant, entry: ClassDashConfigEntry, client: ClassDashClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            # No update_interval: this coordinator is push-driven. See
            # _async_update_data for the one-time exception during setup.
            update_interval=None,
        )
        self.client = client
        self._listen_task: asyncio.Task[None] | None = None
        self._first_update: asyncio.Future[ClassDashData] = hass.loop.create_future()

    async def _async_update_data(self) -> ClassDashData:
        """Called exactly once, by async_config_entry_first_refresh.

        Starts the persistent listener and waits for its first push rather
        than doing a one-off poll — /api/stream sends the current snapshot
        immediately on connect, so this is the real data, not a
        placeholder.
        """
        if self._listen_task is None:
            self._listen_task = self.config_entry.async_create_background_task(
                self.hass, self._listen(), name=f"{DOMAIN}_stream"
            )
        try:
            await asyncio.wait_for(
                asyncio.shield(self._first_update), timeout=STREAM_FIRST_CONNECT_TIMEOUT
            )
            # Deliberately not returning _first_update's own result: a fast
            # reconnect can let the listener push a *second* event (via
            # async_set_updated_data, straight onto self.data) before this
            # coroutine is even rescheduled after the future resolved —
            # `Future.set_result` only schedules its waiters, it doesn't
            # run them immediately. Returning the frozen first-event value
            # in that case would have DataUpdateCoordinator's own
            # `self.data = await self._async_update_data()` clobber the
            # newer data the listener already stored. Reading self.data
            # live sidesteps the race instead of relying on it being rare.
            return self.data if self.data is not None else self._first_update.result()
        except ClassDashAuthError as err:
            # The listen task already saw this and returned on its own —
            # nothing left running to clean up.
            raise ConfigEntryAuthFailed("bearer token rejected") from err
        except ClassDashConnectionError as err:
            # Same here: the listen task set this and returned.
            raise UpdateFailed(str(err)) from err
        except TimeoutError as err:
            # Unlike the two cases above, the listen task is still alive
            # here — the wait_for gave up, not the connection attempt
            # itself. Shielding kept it from being cancelled out from under
            # the task, but that means it's now orphaned unless cancelled
            # explicitly: HA doesn't unload a config entry that never
            # finished setting up, so nothing else would ever stop it.
            if self._listen_task is not None:
                self._listen_task.cancel()
            raise UpdateFailed(
                "timed out waiting for the first update from /api/stream"
            ) from err

    async def _listen(self) -> None:
        """Reconnect forever, with backoff, until the config entry unloads
        (which cancels this task automatically)."""
        backoff = STREAM_RECONNECT_MIN_SECONDS
        while True:
            try:
                async for event in self.client.async_stream_updates():
                    backoff = STREAM_RECONNECT_MIN_SECONDS
                    if event.event == "update":
                        data = _parse_snapshot(event.data)
                        if not self._first_update.done():
                            self._first_update.set_result(data)
                        else:
                            self.async_set_updated_data(data)
                    elif event.event == "heartbeat" and self._first_update.done():
                        # A freshness signal, not new assignment/announcement
                        # data (CONTRIBUTING.md is explicit about that) — swap
                        # in just the refreshed status (collectedAt/minutesAgo
                        # tick even when nothing else has), keep the existing
                        # lists untouched. Guarding on _first_update.done()
                        # rather than `self.data is not None`: self.data is
                        # only set by DataUpdateCoordinator's own assignment
                        # in _async_refresh, which — same race as
                        # _async_update_data's own comment above — may not
                        # have run yet even though _first_update itself
                        # already has a result (set_result() marks a future
                        # done immediately; it only *schedules* waiters,
                        # doesn't run them). Reading self.data with the same
                        # fallback as _async_update_data sidesteps that.
                        base = (
                            self.data
                            if self.data is not None
                            else self._first_update.result()
                        )
                        self.async_set_updated_data(
                            dataclasses.replace(base, status=event.data)
                        )
                # The stream ended without an error (server closed it
                # cleanly) — treat the same as a connection error below:
                # reconnect after a short wait.
                raise ClassDashConnectionError("stream closed")
            except ClassDashAuthError as err:
                if not self._first_update.done():
                    self._first_update.set_exception(err)
                    return
                # A token rolled out from under an already-running stream.
                # Reauth reloads the entry, which replaces this coordinator
                # (and cancels this task) entirely — nothing left to do here.
                self.config_entry.async_start_reauth(self.hass)
                return
            except ClassDashConnectionError as err:
                if not self._first_update.done():
                    self._first_update.set_exception(err)
                    return
                _LOGGER.debug(
                    "classdash stream disconnected, retrying in %ss: %s",
                    backoff,
                    err,
                )
                if backoff >= STREAM_UNAVAILABLE_THRESHOLD_SECONDS:
                    self.async_set_update_error(err)
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
                if not self._first_update.done():
                    self._first_update.set_exception(err)
                    return
                _LOGGER.exception(
                    "classdash stream: unexpected error, retrying in %ss", backoff
                )
                if backoff >= STREAM_UNAVAILABLE_THRESHOLD_SECONDS:
                    self.async_set_update_error(err)

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, STREAM_RECONNECT_MAX_SECONDS)
