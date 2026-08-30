"""Tests for the ClassDash config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

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
