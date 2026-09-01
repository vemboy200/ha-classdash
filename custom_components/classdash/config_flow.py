"""Config flow for ClassDash.

Setup is trust-on-first-use: step one fetches whatever certificate the
server is currently presenting (unverified — there's nothing to verify
against yet) and shows its fingerprint to the user, who is expected to
compare it against the one ClassDash printed to its own terminal on
startup. Only once that's confirmed, together with the bearer token
printed alongside it, does anything get pinned and stored.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_TOKEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    ClassDashAuthError,
    ClassDashClient,
    ClassDashConnectionError,
    build_ssl_context,
    fetch_server_certificate,
    fingerprint_from_der,
    pem_from_der,
)
from .const import CONF_CERT_PEM, DEFAULT_PORT, DOMAIN
from .coordinator import ClassDashConfigEntry

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
    }
)

STEP_TOKEN_SCHEMA = vol.Schema({vol.Required(CONF_TOKEN): str})


async def _validate_token(
    hass: HomeAssistant, host: str, port: int, cert_pem: str, token: str
) -> None:
    """Raise ClassDashAuthError / ClassDashConnectionError if the token is bad."""
    # hass's shared session, same as __init__.py uses once the entry
    # exists — nothing about it is entry-specific; the pinned SSL context
    # is passed per-request instead, so there's no reason for the config
    # flow to open its own separate session.
    ssl_context = await asyncio.get_running_loop().run_in_executor(
        None, build_ssl_context, cert_pem
    )
    session = async_get_clientsession(hass)
    client = ClassDashClient(session, host, port, token, ssl_context)
    await client.async_get_status()


class ClassDashConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for ClassDash."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int | None = None
        self._fingerprint: str | None = None
        self._cert_pem: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First step: where's the server.

        Also the entry point for reconfigure (async_step_reconfigure just
        delegates here) — same form, pre-filled with the entry's current
        host/port when there is one to pre-fill from.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]
            try:
                der = await fetch_server_certificate(host, port)
            except ClassDashConnectionError:
                errors["base"] = "cannot_connect"
            else:
                self._host = host
                self._port = port
                self._fingerprint = fingerprint_from_der(der)
                self._cert_pem = pem_from_der(der)
                return await self.async_step_confirm()

        schema = STEP_USER_SCHEMA
        if self.source == SOURCE_RECONFIGURE:
            current = self._get_reconfigure_entry().data
            schema = self.add_suggested_values_to_schema(
                STEP_USER_SCHEMA,
                {CONF_HOST: current[CONF_HOST], CONF_PORT: current[CONF_PORT]},
            )

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change host/port without removing and re-adding the entry.

        Runs the exact same two steps as initial setup (fetch the cert,
        confirm its fingerprint, enter a token) rather than a cut-down
        version — a different address might be a genuinely different
        server, needing its own pinned certificate and token, not just a
        moved copy of the same one.
        """
        return await self.async_step_user(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Second step: confirm the fingerprint and provide the token."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            try:
                await _validate_token(
                    self.hass, self._host, self._port, self._cert_pem, token
                )
            except ClassDashAuthError:
                errors["base"] = "invalid_auth"
            except ClassDashConnectionError:
                errors["base"] = "cannot_connect"
            else:
                new_unique_id = f"{self._host}:{self._port}"
                await self.async_set_unique_id(new_unique_id)
                data = {
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_TOKEN: token,
                    CONF_CERT_PEM: self._cert_pem,
                }
                if self.source == SOURCE_RECONFIGURE:
                    reconfigure_entry = self._get_reconfigure_entry()
                    if new_unique_id != reconfigure_entry.unique_id:
                        # Only a genuine problem if it collides with some
                        # *other* entry — matching the entry being
                        # reconfigured itself (unchanged host/port) is
                        # the common case and must not abort.
                        self._abort_if_unique_id_configured()
                    return self.async_update_reload_and_abort(
                        reconfigure_entry, unique_id=new_unique_id, data=data
                    )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"ClassDash ({self._host})", data=data
                )

        return self.async_show_form(
            step_id="confirm",
            data_schema=STEP_TOKEN_SCHEMA,
            errors=errors,
            description_placeholders={"fingerprint": self._fingerprint or ""},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Entered when a stored token stops working (e.g. it was rolled)."""
        entry: ClassDashConfigEntry = self._get_reauth_entry()
        self._host = entry.data[CONF_HOST]
        self._port = entry.data[CONF_PORT]
        self._cert_pem = entry.data[CONF_CERT_PEM]
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new token; the pinned certificate doesn't change."""
        errors: dict[str, str] = {}
        if user_input is not None:
            token = user_input[CONF_TOKEN]
            try:
                await _validate_token(
                    self.hass, self._host, self._port, self._cert_pem, token
                )
            except ClassDashAuthError:
                errors["base"] = "invalid_auth"
            except ClassDashConnectionError:
                errors["base"] = "cannot_connect"
            else:
                entry = self._get_reauth_entry()
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_TOKEN: token}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_TOKEN_SCHEMA,
            errors=errors,
        )
