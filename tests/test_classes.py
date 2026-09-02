"""End-to-end tests for per-class devices: created dynamically from live
data, linked to the main device, and holding both the count sensors and
the calendar entity."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.classdash.api import StreamEvent
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from custom_components.classdash.devices import class_unique_id
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import bundle_extras


def _assignment(class_name: str, title: str, due: str, item_id: str, tags=()) -> dict:
    return {
        "id": item_id,
        "title": title,
        "class": class_name,
        "due": due,
        "link": None,
        "tags": list(tags),
    }


def _status(**counts) -> dict:
    return {
        "collectedAt": "2026-08-30T12:00:00.000Z",
        "minutesAgo": 1,
        "classes": 2,
        "total": sum(counts.values()),
        "dueSoon": counts.get("due_soon", 0),
        "overdue": counts.get("overdue", 0),
        "ahead": counts.get("ahead", 0),
        "done": counts.get("done", 0),
        "announcements": counts.get("announcements", 0),
        "removed": 0,
        "language": "en",
    }


def _bundle(
    due_soon=(), ahead=(), overdue=(), done=(), announcements=(), classes=(), virtual=()
) -> dict:
    return {
        **bundle_extras(),
        "status": _status(
            due_soon=len(due_soon),
            ahead=len(ahead),
            overdue=len(overdue),
            done=len(done),
            announcements=len(announcements),
        ),
        "due-soon": list(due_soon),
        "ahead": list(ahead),
        "overdue": list(overdue),
        "done": list(done),
        "announcements": list(announcements),
        "classes": list(classes),
        "virtual": list(virtual),
    }


def _make_entry(sample_certificate) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={
            "host": "192.168.1.50",
            "port": 8734,
            "token": "a" * 64,
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )


async def test_class_devices_created_with_correct_entities_and_linkage(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-10T23:59:00+00:00", "p1")],
        overdue=[
            _assignment(
                "AP Chem, Period 2!", "Worksheet", "2026-08-20T23:59:00+00:00", "c1"
            )
        ],
    )

    async def fake_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    main_device = dev_reg.async_get_device({(DOMAIN, entry.unique_id)})
    assert main_device is not None

    physics_device = dev_reg.async_get_device(
        {(DOMAIN, class_unique_id(entry, "Physics"))}
    )
    assert physics_device is not None
    assert physics_device.via_device_id == main_device.id
    assert physics_device.name == "Physics"

    chem_device = dev_reg.async_get_device(
        {(DOMAIN, class_unique_id(entry, "AP Chem, Period 2!"))}
    )
    assert chem_device is not None
    assert chem_device.via_device_id == main_device.id

    def _state_for(unique_id: str, domain: str = "sensor"):
        entity_id = ent_reg.async_get_entity_id(domain, DOMAIN, unique_id)
        assert entity_id is not None, f"no entity registered for {unique_id}"
        return hass.states.get(entity_id)

    physics_due_soon = _state_for(f"{class_unique_id(entry, 'Physics')}_due_soon")
    assert physics_due_soon.state == "1"
    physics_overdue = _state_for(f"{class_unique_id(entry, 'Physics')}_overdue")
    assert physics_overdue.state == "0"

    chem_overdue = _state_for(f"{class_unique_id(entry, 'AP Chem, Period 2!')}_overdue")
    assert chem_overdue.state == "1"

    physics_calendar = _state_for(
        f"{class_unique_id(entry, 'Physics')}_calendar", domain="calendar"
    )
    assert physics_calendar is not None
    assert physics_calendar.attributes["message"] == "Lab report"


async def test_a_class_appearing_later_gets_its_own_entities(
    hass: HomeAssistant, sample_certificate
) -> None:
    """No entry reload needed — a class showing up for the first time in a
    later push should get entities immediately."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    first = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-10T23:59:00+00:00", "p1")]
    )
    second = _bundle(
        due_soon=[
            _assignment("Physics", "Lab report", "2026-09-10T23:59:00+00:00", "p1"),
            _assignment("Biology", "Reading", "2026-09-02T23:59:00+00:00", "b1"),
        ]
    )
    second_pushed = asyncio.Event()

    async def fake_stream():
        yield StreamEvent("update", first)
        yield StreamEvent("update", second)
        second_pushed.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(second_pushed.wait(), timeout=2)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{class_unique_id(entry, 'Biology')}_due_soon"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "1"


async def test_class_with_nothing_due_still_gets_a_device(
    hass: HomeAssistant, sample_certificate
) -> None:
    """A class can appear in /api/classes' roster (ClassDash's own
    showEmptyClasses setting) with zero current items — it should still
    get a device, with its sensors reading 0 and no calendar events,
    rather than being invisible until something's assigned."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-10T23:59:00+00:00", "p1")],
        classes=[
            {"name": "Physics", "dueSoon": 1, "ahead": 0, "overdue": 0},
            {"name": "Art History", "dueSoon": 0, "ahead": 0, "overdue": 0},
        ],
    )

    async def fake_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    art_device = dev_reg.async_get_device(
        {(DOMAIN, class_unique_id(entry, "Art History"))}
    )
    assert art_device is not None

    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{class_unique_id(entry, 'Art History')}_due_soon"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "0"

    calendar_id = ent_reg.async_get_entity_id(
        "calendar", DOMAIN, f"{class_unique_id(entry, 'Art History')}_calendar"
    )
    assert calendar_id is not None
    assert hass.states.get(calendar_id).state == "off"


async def test_orphaned_class_gets_no_device_even_with_lingering_items(
    hass: HomeAssistant, sample_certificate
) -> None:
    """The exact scenario ClassDash's CONTRIBUTING.md calls out by name:
    a class the student has moved on from, but that still has old items
    sitting in overdue, must not get an entity."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    bundle = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-10T23:59:00+00:00", "p1")],
        overdue=[_assignment("Old Class", "Ancient worksheet", "2020-01-01T00:00:00+00:00", "o1")],
        classes=[
            {"name": "Physics", "dueSoon": 1, "ahead": 0, "overdue": 0, "status": "known"},
            {
                "name": "Old Class",
                "dueSoon": 0,
                "ahead": 0,
                "overdue": 1,
                "status": "orphaned",
            },
        ],
    )

    async def fake_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    assert (
        dev_reg.async_get_device({(DOMAIN, class_unique_id(entry, "Old Class"))})
        is None
    )


async def test_hidden_items_excluded_from_per_class_count_and_calendar(
    hass: HomeAssistant, sample_certificate
) -> None:
    """/api/overdue no longer filters hidden items server-side (they're
    tagged instead) — the per-class sensor and calendar must filter them
    out themselves, or a dismissed item would silently inflate the count
    and reappear on the calendar."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    bundle = _bundle(
        overdue=[
            _assignment(
                "Physics", "Visible one", "2026-09-10T00:00:00+00:00", "v1"
            ),
            _assignment(
                "Physics",
                "Dismissed one",
                "2026-09-11T00:00:00+00:00",
                "h1",
                tags=["hidden"],
            ),
        ]
    )

    async def fake_stream():
        yield StreamEvent("update", bundle)
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)

    overdue_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{class_unique_id(entry, 'Physics')}_overdue"
    )
    overdue_state = hass.states.get(overdue_id)
    assert overdue_state.state == "1"
    assert [a["title"] for a in overdue_state.attributes["assignments"]] == [
        "Visible one"
    ]
    # id is included specifically so it's discoverable for the
    # classdash.hide/mute services, which need it and have no other
    # reasonable way to show it.
    assert overdue_state.attributes["assignments"][0]["id"] == "v1"

    calendar_id = ent_reg.async_get_entity_id(
        "calendar", DOMAIN, f"{class_unique_id(entry, 'Physics')}_calendar"
    )
    assert (
        hass.states.get(calendar_id).attributes["message"] == "Visible one (overdue)"
    )


async def test_class_device_removed_when_class_disappears(
    hass: HomeAssistant, sample_certificate
) -> None:
    """A class that stops appearing anywhere (orphaned, excluded, gone
    stale on ClassDash's own side) should have its device — and by
    extension all its entities — actually removed, not just left
    forever."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    with_physics = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-10T00:00:00+00:00", "p1")]
    )
    without_physics = _bundle()
    gone = asyncio.Event()

    async def fake_stream():
        yield StreamEvent("update", with_physics)
        yield StreamEvent("update", without_physics)
        gone.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(gone.wait(), timeout=2)
        await hass.async_block_till_done()

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    assert (
        dev_reg.async_get_device({(DOMAIN, class_unique_id(entry, "Physics"))})
        is None
    )
    assert (
        ent_reg.async_get_entity_id(
            "sensor", DOMAIN, f"{class_unique_id(entry, 'Physics')}_due_soon"
        )
        is None
    )
    assert (
        ent_reg.async_get_entity_id(
            "calendar", DOMAIN, f"{class_unique_id(entry, 'Physics')}_calendar"
        )
        is None
    )
    # The main device must never be swept up in this.
    assert dev_reg.async_get_device({(DOMAIN, entry.unique_id)}) is not None


async def test_class_device_recreated_after_disappearing_and_reappearing(
    hass: HomeAssistant, sample_certificate
) -> None:
    """The actual bug this is a regression test for: the "add new class"
    logic used to track "classes I've ever added" in a plain set that
    only ever grew, so once a class's device was removed, a later
    reappearance was silently ignored forever — it now checks the entity
    registry directly instead, which reflects reality."""
    entry = _make_entry(sample_certificate)
    entry.add_to_hass(hass)

    appears = _bundle(
        due_soon=[_assignment("Physics", "Lab report", "2026-09-10T00:00:00+00:00", "p1")]
    )
    disappears = _bundle()
    reappears = _bundle(
        due_soon=[_assignment("Physics", "New assignment", "2026-09-20T00:00:00+00:00", "p2")]
    )
    gone = asyncio.Event()
    resume_after_check = asyncio.Event()
    back = asyncio.Event()

    async def fake_stream():
        yield StreamEvent("update", appears)
        yield StreamEvent("update", disappears)
        gone.set()
        # A genuine suspension point (unlike `gone.set(); await gone.wait()`
        # on the same already-set event, which wouldn't actually block) —
        # without this, nothing stops the generator from racing straight
        # through to "reappears" before the test below ever gets a chance
        # to observe the removed state in between.
        await resume_after_check.wait()
        yield StreamEvent("update", reappears)
        back.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.classdash.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_stream_updates = fake_stream
        assert await hass.config_entries.async_setup(entry.entry_id)
        await asyncio.wait_for(gone.wait(), timeout=2)
        await hass.async_block_till_done()

        dev_reg = dr.async_get(hass)
        assert (
            dev_reg.async_get_device({(DOMAIN, class_unique_id(entry, "Physics"))})
            is None
        )

        resume_after_check.set()
        await asyncio.wait_for(back.wait(), timeout=2)
        await hass.async_block_till_done()

    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id(
        "sensor", DOMAIN, f"{class_unique_id(entry, 'Physics')}_due_soon"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).state == "1"
