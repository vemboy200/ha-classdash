"""Tests for the ClassDash config flow."""

from __future__ import annotations

from ipaddress import ip_address
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from custom_components.classdash.api import ClassDashAuthError, ClassDashConnectionError
from custom_components.classdash.const import CONF_CERT_PEM, DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

USER_INPUT = {"host": "192.168.1.50", "port": 8734}
TOKEN_INPUT = {"token": "a" * 64}


@pytest.fixture
def mock_fetch_cert(sample_certificate):
    """Patch the trust-on-first-use fetch to return a known certificate."""
    with patch(
        "custom_components.classdash.config_flow.fetch_server_certificate",
        AsyncMock(return_value=sample_certificate.der),
    ):
        yield sample_certificate


@pytest.fixture
def mock_status_ok():
    """Patch the client used to validate a token so it just succeeds."""
    with patch(
        "custom_components.classdash.config_flow.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_get_status = AsyncMock(return_value={})
        yield mock_client_cls


async def test_user_step_shows_form(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_cannot_connect_on_user_step(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.classdash.config_flow.fetch_server_certificate",
        AsyncMock(side_effect=ClassDashConnectionError("refused")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "cannot_connect"}


async def test_confirm_step_shows_fingerprint(
    hass: HomeAssistant, mock_fetch_cert
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["fingerprint"] == (
        mock_fetch_cert.fingerprint
    )


async def test_invalid_auth_on_confirm_step(
    hass: HomeAssistant, mock_fetch_cert
) -> None:
    with patch(
        "custom_components.classdash.config_flow.ClassDashClient", autospec=True
    ) as mock_client_cls:
        mock_client_cls.return_value.async_get_status = AsyncMock(
            side_effect=ClassDashAuthError("nope")
        )
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], TOKEN_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_full_flow_creates_entry(
    hass: HomeAssistant, mock_fetch_cert, mock_status_ok, mock_setup_entry: AsyncMock
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], TOKEN_INPUT
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == USER_INPUT["host"]
    assert result["data"]["port"] == USER_INPUT["port"]
    assert result["data"]["token"] == TOKEN_INPUT["token"]
    assert result["data"][CONF_CERT_PEM] == mock_fetch_cert.pem
    assert mock_setup_entry.called


async def test_duplicate_host_port_aborts(
    hass: HomeAssistant, mock_fetch_cert, mock_status_ok, mock_setup_entry: AsyncMock
) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{USER_INPUT['host']}:{USER_INPUT['port']}",
        data={**USER_INPUT, "token": "old", CONF_CERT_PEM: mock_fetch_cert.pem},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], TOKEN_INPUT
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reconfigure_updates_host_and_reprompts_for_fingerprint(
    hass: HomeAssistant, mock_status_ok, mock_setup_entry: AsyncMock, sample_certificate
) -> None:
    """Reconfigure runs the exact same two steps as initial setup — a
    different address gets its own fingerprint confirmation and token,
    not just a silent host swap on the existing pinned cert."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{USER_INPUT['host']}:{USER_INPUT['port']}",
        data={**USER_INPUT, "token": "old-token", CONF_CERT_PEM: sample_certificate.pem},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "user"
    # Pre-filled with the entry's current values — a suggested_value
    # hint per field, not a functional default (voluptuous itself
    # still requires real input; this is UI pre-fill only).
    suggested = {
        key: key.description["suggested_value"] for key in result["data_schema"].schema
    }
    assert suggested == USER_INPUT

    new_host_input = {"host": "192.168.1.99", "port": 8734}
    with patch(
        "custom_components.classdash.config_flow.fetch_server_certificate",
        AsyncMock(return_value=sample_certificate.der),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], new_host_input
        )
    assert result["step_id"] == "confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "new-token"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "192.168.1.99"
    assert entry.data["token"] == "new-token"
    assert entry.unique_id == "192.168.1.99:8734"


async def test_reconfigure_to_same_address_does_not_abort_as_duplicate(
    hass: HomeAssistant, mock_status_ok, mock_setup_entry: AsyncMock, sample_certificate
) -> None:
    """Reconfiguring without changing host/port must not trip the
    duplicate-entry check against itself."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{USER_INPUT['host']}:{USER_INPUT['port']}",
        data={**USER_INPUT, "token": "old-token", CONF_CERT_PEM: sample_certificate.pem},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.classdash.config_flow.fetch_server_certificate",
        AsyncMock(return_value=sample_certificate.der),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "new-token"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["token"] == "new-token"


async def test_reconfigure_to_another_entrys_address_aborts_as_duplicate(
    hass: HomeAssistant, mock_status_ok, mock_setup_entry: AsyncMock, sample_certificate
) -> None:
    """Changing to an address *another* existing entry already uses is a
    real collision, unlike matching yourself."""
    other = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.200:8734",
        data={
            "host": "192.168.1.200",
            "port": 8734,
            "token": "t",
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )
    other.add_to_hass(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{USER_INPUT['host']}:{USER_INPUT['port']}",
        data={**USER_INPUT, "token": "old-token", CONF_CERT_PEM: sample_certificate.pem},
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with patch(
        "custom_components.classdash.config_flow.fetch_server_certificate",
        AsyncMock(return_value=sample_certificate.der),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"host": "192.168.1.200", "port": 8734}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "new-token"}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_token(
    hass: HomeAssistant, mock_status_ok, mock_setup_entry: AsyncMock, sample_certificate
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{USER_INPUT['host']}:{USER_INPUT['port']}",
        data={
            **USER_INPUT,
            "token": "expired-token",
            CONF_CERT_PEM: sample_certificate.pem,
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"token": "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == "new-token"
    assert entry.data[CONF_CERT_PEM] == sample_certificate.pem


def _discovery(host: str = "192.168.1.60", port: int = 8734, fp: str = "") -> ZeroconfServiceInfo:
    return ZeroconfServiceInfo(
        ip_address=ip_address(host),
        ip_addresses=[ip_address(host)],
        hostname="classdash-laptop.local.",
        name="ClassDash on Laptop._classdash._tcp.local.",
        port=port,
        type="_classdash._tcp.local.",
        properties={"api": "1", "fp": fp},
    )


async def test_zeroconf_discovers_a_new_classdash(
    hass: HomeAssistant, mock_fetch_cert, mock_status_ok, mock_setup_entry
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_discovery(fp=mock_fetch_cert.fingerprint),
    )
    # Straight to the token: the address came from the announcement.
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm"
    assert result["description_placeholders"]["fingerprint"] == mock_fetch_cert.fingerprint
    flow = hass.config_entries.flow.async_get(result["flow_id"])
    assert flow["context"]["title_placeholders"] == {"name": "ClassDash on Laptop"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], TOKEN_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "192.168.1.60"
    assert result["data"]["port"] == 8734
    assert result["data"][CONF_CERT_PEM] == mock_fetch_cert.pem
    assert result["result"].unique_id == "192.168.1.60:8734"


async def test_zeroconf_cannot_connect_aborts(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.classdash.config_flow.fetch_server_certificate",
        AsyncMock(side_effect=ClassDashConnectionError("refused")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_ZEROCONF},
            data=_discovery(),
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "cannot_connect"


async def test_zeroconf_follows_a_known_classdash_to_a_new_address(
    hass: HomeAssistant, sample_certificate, mock_setup_entry
) -> None:
    """Recognized by its certificate, not its address, so a new address
    updates the entry (and reloads it) instead of offering a second one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={**USER_INPUT, **TOKEN_INPUT, CONF_CERT_PEM: sample_certificate.pem},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_discovery("192.168.1.60", 8735, fp=sample_certificate.fingerprint),
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "192.168.1.60"
    assert entry.data["port"] == 8735
    assert entry.data["token"] == TOKEN_INPUT["token"]
    assert mock_setup_entry.call_count == 1


async def test_zeroconf_at_the_same_address_reconnects_now(
    hass: HomeAssistant, sample_certificate
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={**USER_INPUT, **TOKEN_INPUT, CONF_CERT_PEM: sample_certificate.pem},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
    entry.runtime_data = MagicMock()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_discovery("192.168.1.50", 8734, fp=sample_certificate.fingerprint),
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    entry.runtime_data.async_reconnect_now.assert_called_once()
    assert entry.data["host"] == "192.168.1.50"


async def test_zeroconf_for_another_certificate_is_a_new_classdash(
    hass: HomeAssistant, sample_certificate, mock_fetch_cert
) -> None:
    """A different fingerprint is a different ClassDash, even at an
    address an entry once had: it isn't moved onto that entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="192.168.1.50:8734",
        data={**USER_INPUT, **TOKEN_INPUT, CONF_CERT_PEM: sample_certificate.pem},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_ZEROCONF},
        data=_discovery("192.168.1.60", fp="AA:BB"),
    )
    assert result["step_id"] == "confirm"
    assert entry.data["host"] == "192.168.1.50"
