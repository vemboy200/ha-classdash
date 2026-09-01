"""Tests for diagnostics — mainly that the token and pinned certificate
actually get redacted, since that's the whole point of having this be
narrow rather than just dumping everything."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.classdash import diagnostics
from custom_components.classdash.api import StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras

FAKE_BUNDLE = {
    **bundle_extras(),
    "status": {
        "collectedAt": "2026-09-01T00:00:00.000Z",
        "minutesAgo": 1,
        "classes": 1,
        "total": 1,
        "dueSoon": 1,
        "overdue": 0,
        "ahead": 0,
        "done": 0,
        "announcements": 0,
        "removed": 0,
        "language": "en",
    },
    "due-soon": [
        {
            "id": "a1",
            "title": "Secret assignment title",
            "class": "Physics",
            "due": "2026-09-10T00:00:00+00:00",
            "link": None,
            "tags": [],
        }
    ],
    "ahead": [],
    "overdue": [],
    "announcements": [],
    "classes": [{"name": "Physics", "dueSoon": 1, "ahead": 0, "overdue": 0, "status": "known"}],
}


async def test_diagnostics_redacts_token_and_cert(
    hass: HomeAssistant, sample_certificate
) -> None:
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

    async def fake_stream():
        yield StreamEvent("update", FAKE_BUNDLE)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["host"] == "192.168.1.50"
    assert result["entry"]["port"] == 8734
    assert result["entry"]["token"] == "**REDACTED**"
    assert result["entry"][CONF_CERT_PEM] == "**REDACTED**"

    assert result["coordinator"]["last_update_success"] is True
    assert result["coordinator"]["counts"]["due_soon"] == 1
    assert result["coordinator"]["classes"] == FAKE_BUNDLE["classes"]

    # The actual assignment title/content must never appear anywhere in
    # the dump — that's the whole reason counts+classes were chosen
    # instead of the raw item lists.
    dump = repr(result)
    assert "Secret assignment title" not in dump
