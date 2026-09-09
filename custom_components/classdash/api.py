"""Thin async client for the ClassDash home API.

The server is self-signed with no real CA behind it (a private LAN address
has no business getting a publicly-trusted certificate), so normal
hostname/CA validation doesn't apply. Instead, `build_ssl_context` pins
trust to the *exact* certificate the config flow fetched on setup — that
certificate is loaded as if it were a trusted CA, and nothing else will
verify against it. This mirrors the pinning ClassDash's own README asks
any client to do; see `fetch_server_certificate` for the trust-on-first-use
step that captures the certificate in the first place.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import ssl
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import aiohttp

# ClassDash sends a heartbeat every 60s (see CONTRIBUTING.md) even when
# nothing changed, specifically so a push-only client can tell "checked,
# genuinely nothing new" apart from "stopped running an hour ago". This
# read timeout gives real margin above that before treating the
# connection as dead and reconnecting.
STREAM_READ_TIMEOUT = 90


class ClassDashError(Exception):
    """Base error talking to a ClassDash server."""


class ClassDashConnectionError(ClassDashError):
    """Could not reach the server at all."""


class ClassDashAuthError(ClassDashError):
    """Reached the server, but the bearer token was missing or wrong."""


class ClassDashValidationError(ClassDashError):
    """The server rejected the input itself — a 400 with either
    {"ok": false, "why": "..."} (create/edit's own validation, e.g. a
    missing title or an unparseable due date) or {"error": "..."} (the
    handler's own request-shape check, e.g. a body with no "id" at all).
    Worth its own error, distinct from ClassDashConnectionError: this
    means the server understood the request just fine and said no, not
    that it couldn't be reached."""


async def fetch_server_certificate(host: str, port: int) -> bytes:
    """Open a bare TLS connection and return the peer certificate (DER).

    Deliberately does not verify anything — this is the trust-on-first-use
    step. The caller is expected to show the resulting fingerprint to a
    human, who compares it against the one ClassDash printed to its own
    terminal on startup before it's trusted for anything further.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ctx), timeout=10
        )
    except (OSError, asyncio.TimeoutError) as err:
        raise ClassDashConnectionError(str(err)) from err

    try:
        ssl_object = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True)
        if der is None:
            raise ClassDashConnectionError("server did not present a certificate")
        return der
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def fingerprint_from_der(der: bytes) -> str:
    """SHA-256 fingerprint, formatted the same way ClassDash prints its own."""
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def pem_from_der(der: bytes) -> str:
    """Convert a DER certificate to PEM, for use as pinned CA data."""
    return ssl.DER_cert_to_PEM_cert(der)


def build_ssl_context(cert_pem: str) -> ssl.SSLContext:
    """Build an SSLContext that trusts only the pinned certificate.

    Runs synchronously and does no disk/network I/O (cadata is in-memory),
    but callers should still route it through the executor since it's not
    async-native.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cadata=cert_pem)
    return ctx


@dataclass(frozen=True)
class StreamEvent:
    """One event off /api/stream, tagged with which of the two kinds it is.

    "update" carries a full bundled snapshot (every REST handle's output).
    "heartbeat" carries only /api/status — a freshness signal, not new
    assignment/announcement data; see CONTRIBUTING.md's explicit warning
    against merging it in as if it were.
    """

    event: str
    data: dict[str, Any]


class ClassDashClient:
    """Talks to one ClassDash home API instance."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        port: int,
        token: str,
        ssl_context: ssl.SSLContext,
    ) -> None:
        self._session = session
        self._base = f"https://{host}:{port}"
        self._token = token
        self._ssl_context = ssl_context

    async def _get(self, path: str) -> Any:
        try:
            async with self._session.get(
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                ssl=self._ssl_context,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    raise ClassDashAuthError("missing or wrong bearer token")
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise ClassDashConnectionError(str(err)) from err

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> None:
        try:
            async with self._session.post(
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                ssl=self._ssl_context,
                json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    raise ClassDashAuthError("missing or wrong bearer token")
                resp.raise_for_status()
        except aiohttp.ClientError as err:
            raise ClassDashConnectionError(str(err)) from err

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Like _post, but for the virtual-reminder create/edit endpoints,
        which return a real body — {"ok": true, "entry": {...}} on
        success. A 400 means the server understood the request and
        rejected it (missing title, unparseable due date, or a
        malformed body entirely) — surfaced as ClassDashValidationError
        with whatever message the server gave, rather than folding it
        into the generic "couldn't reach the server" error."""
        try:
            async with self._session.post(
                f"{self._base}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                ssl=self._ssl_context,
                json=body,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    raise ClassDashAuthError("missing or wrong bearer token")
                if resp.status == 400:
                    data = await resp.json()
                    raise ClassDashValidationError(
                        data.get("why") or data.get("error") or "invalid input"
                    )
                resp.raise_for_status()
                return await resp.json()
        except aiohttp.ClientError as err:
            raise ClassDashConnectionError(str(err)) from err

    async def async_get_status(self) -> dict[str, Any]:
        """A single quick round trip — used only to validate a token in the
        config flow. Ongoing data comes from `async_stream_updates` instead."""
        return await self._get("/api/status")

    async def async_reload(self) -> None:
        """Start a quick collection pass (Classroom + Canvas, ~17s).

        Only starts it — this returns as soon as ClassDash has accepted the
        request, not when the pass finishes. The push stream's next
        "update" event (or /api/status's collectedAt/minutesAgo) is how a
        caller finds out when it's actually done.
        """
        await self._post("/api/reload")

    async def async_check(self) -> None:
        """Start the full collection pass (+ Edpuzzle, ~1 min). Same
        fire-and-forget shape as async_reload."""
        await self._post("/api/check")

    async def async_hide(self, item_id: str) -> None:
        """Dismiss an assignment — the same "hide" a click on ClassDash's
        own summary page does. `item_id` is whatever id the assignment
        came back with (e.g. from /api/overdue)."""
        await self._post("/api/hide", {"id": item_id})

    async def async_unhide(self, item_id: str) -> None:
        await self._post("/api/unhide", {"id": item_id})

    async def async_mute(self, item_id: str) -> None:
        """Mark "not urgent" — still counted, just not badged/notified on."""
        await self._post("/api/mute", {"id": item_id})

    async def async_unmute(self, item_id: str) -> None:
        await self._post("/api/unmute", {"id": item_id})

    async def async_get_virtual(self) -> list[dict[str, Any]]:
        """The current virtual-reminder list — /api/virtual. Used to
        refresh the coordinator right after a create/edit, since
        virtual-assignments.json (unlike a real collection pass) isn't
        one of the files /api/stream watches for changes — see
        async_create_virtual_reminder/async_edit_virtual_reminder."""
        return await self._get("/api/virtual")

    async def async_create_virtual_reminder(
        self, title: str, class_name: str | None = None, due: str | None = None
    ) -> dict[str, Any]:
        """Create a new virtual reminder — /api/virtual/create. `class_name`
        is free text, not checked against any known class roster (a
        reminder can be about a class ClassDash doesn't otherwise track
        at all). `due`, if given, should be an ISO 8601 datetime string;
        ClassDash accepts anything JS's `new Date()` parses, but this
        integration always sends ISO. Returns the created entry."""
        return await self._post_json(
            "/api/virtual/create", {"title": title, "class": class_name, "due": due}
        )

    async def async_edit_virtual_reminder(
        self,
        item_id: str,
        title: str,
        class_name: str | None = None,
        due: str | None = None,
    ) -> dict[str, Any]:
        """Edit an existing virtual reminder — /api/virtual/edit. title,
        class_name, and due are always sent as a full replacement, not a
        partial diff — ClassDash's own edit() overwrites all three every
        time, including clearing class/due back to null if omitted, same
        as the reminder edit form on ClassDash's own page does."""
        return await self._post_json(
            "/api/virtual/edit",
            {"id": item_id, "title": title, "class": class_name, "due": due},
        )

    async def async_dismiss_update(self, version: str) -> None:
        """Mark `version` seen-and-dismissed — the same thing ClassDash's
        own update banner's dismiss button does. A no-op server-side if no
        check has ever run yet."""
        await self._post("/api/update-status/dismiss", {"version": version})

    async def async_stream_updates(self) -> AsyncIterator[StreamEvent]:
        """Connect to /api/stream and yield each event as it arrives.

        ClassDash sends an "update" event immediately on connect (a full
        bundled snapshot), then again only when a collection pass actually
        changed something, plus a "heartbeat" event every 60s regardless
        (just /api/status) so a client can tell a genuinely quiet stretch
        apart from a dead connection. This generator runs for as long as
        the connection lasts and yields once per event; the caller is
        responsible for reconnecting when it ends.

        A read timeout (STREAM_READ_TIMEOUT, well above the 60s heartbeat
        interval) catches a connection that's gone quiet for longer than
        any legitimate gap between events — surfaced as
        ClassDashConnectionError, same as any other connection failure.
        """
        try:
            async with self._session.get(
                f"{self._base}/api/stream",
                headers={"Authorization": f"Bearer {self._token}"},
                ssl=self._ssl_context,
                timeout=aiohttp.ClientTimeout(
                    total=None, sock_connect=10, sock_read=STREAM_READ_TIMEOUT
                ),
            ) as resp:
                if resp.status == 401:
                    raise ClassDashAuthError("missing or wrong bearer token")
                resp.raise_for_status()

                event_type: str | None = None
                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8").rstrip("\n")
                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:") and event_type in (
                        "update",
                        "heartbeat",
                    ):
                        yield StreamEvent(
                            event_type, json.loads(line[len("data:") :].strip())
                        )
                    elif not line:
                        event_type = None
        except aiohttp.ClientError as err:
            raise ClassDashConnectionError(str(err)) from err
