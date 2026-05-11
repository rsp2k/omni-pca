"""Config flow for the HAI/Leviton Omni Panel integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PORT

from omni_pca.client import OmniClient
from omni_pca.connection import (
    ConnectionError as OmniConnectionError,
)
from omni_pca.connection import (
    HandshakeError,
    InvalidEncryptionKeyError,
)

from .const import (
    CONF_CONTROLLER_KEY,
    CONF_TRANSPORT,
    CONTROLLER_KEY_HEX_LEN,
    DEFAULT_PORT,
    DEFAULT_TRANSPORT,
    DOMAIN,
    LOGGER,
    TRANSPORT_TCP,
    TRANSPORT_UDP,
)


class InvalidControllerKey(ValueError):  # noqa: N818 - public surface, predates rule
    """The supplied controller key is not 32 hex characters."""


def parse_controller_key(raw: str) -> bytes:
    """Validate and decode a 32-char hex controller key into 16 raw bytes.

    Pure function so it can be unit-tested without a HA harness.
    Whitespace and a leading ``0x`` prefix are tolerated; case-insensitive.
    """
    if not isinstance(raw, str):
        raise InvalidControllerKey("controller key must be a string")
    cleaned = raw.strip().replace(" ", "").replace(":", "").replace("-", "")
    if cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) != CONTROLLER_KEY_HEX_LEN:
        raise InvalidControllerKey(
            f"controller key must be {CONTROLLER_KEY_HEX_LEN} hex characters "
            f"(got {len(cleaned)})"
        )
    try:
        return bytes.fromhex(cleaned)
    except ValueError as err:
        raise InvalidControllerKey(f"controller key is not valid hex: {err}") from err


_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=65535)
        ),
        vol.Required(CONF_CONTROLLER_KEY): str,
        # Most modern firmware uses TCP; some installers configure
        # Network_UDP. PC Access stores the choice as
        # enuPreferredNetworkProtocol in the .pca config.
        vol.Required(CONF_TRANSPORT, default=DEFAULT_TRANSPORT): vol.In(
            [TRANSPORT_TCP, TRANSPORT_UDP]
        ),
    }
)


class OmniConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for omni_pca."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry_data: Mapping[str, Any] | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host: str = user_input[CONF_HOST].strip()
            port: int = user_input[CONF_PORT]
            transport: str = user_input.get(CONF_TRANSPORT, DEFAULT_TRANSPORT)
            unique_id = f"{host}:{port}"

            await self.async_set_unique_id(unique_id)
            self._abort_if_unique_id_configured()

            try:
                key = parse_controller_key(user_input[CONF_CONTROLLER_KEY])
            except InvalidControllerKey as err:
                LOGGER.debug("controller key rejected: %s", err)
                errors[CONF_CONTROLLER_KEY] = "invalid_key"
            else:
                title, error = await self._probe(host, port, key, transport)
                if error is not None:
                    errors["base"] = error
                else:
                    return self.async_create_entry(
                        title=title or f"Omni Panel ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_CONTROLLER_KEY: key.hex(),
                            CONF_TRANSPORT: transport,
                        },
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        self._reauth_entry_data = entry_data
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._reauth_entry_data is not None
        host: str = self._reauth_entry_data[CONF_HOST]
        port: int = self._reauth_entry_data[CONF_PORT]
        transport: str = self._reauth_entry_data.get(
            CONF_TRANSPORT, DEFAULT_TRANSPORT
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                key = parse_controller_key(user_input[CONF_CONTROLLER_KEY])
            except InvalidControllerKey:
                errors[CONF_CONTROLLER_KEY] = "invalid_key"
            else:
                _, error = await self._probe(host, port, key, transport)
                if error is not None:
                    errors["base"] = error
                else:
                    entry = self._get_reauth_entry()
                    new_data = {**entry.data, CONF_CONTROLLER_KEY: key.hex()}
                    return self.async_update_reload_and_abort(entry, data=new_data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_CONTROLLER_KEY): str}),
            description_placeholders={"host": host, "port": str(port)},
            errors=errors,
        )

    # ---- helpers ---------------------------------------------------------

    async def _probe(
        self,
        host: str,
        port: int,
        key: bytes,
        transport: str = DEFAULT_TRANSPORT,
    ) -> tuple[str | None, str | None]:
        """Try to connect once. Returns (title, error_code)."""
        try:
            async with OmniClient(
                host,
                port=port,
                controller_key=key,
                transport=transport,  # type: ignore[arg-type]
            ) as client:
                info = await client.get_system_information()
        except (HandshakeError, InvalidEncryptionKeyError):
            return None, "invalid_auth"
        except (OmniConnectionError, OSError, TimeoutError) as err:
            LOGGER.debug("probe connect failed: %s", err)
            return None, "cannot_connect"
        except Exception:
            LOGGER.exception("unexpected probe failure")
            return None, "unknown"
        return f"{info.model_name} ({host})", None

    def _get_reauth_entry(self):  # type: ignore[no-untyped-def]
        """Resolve the entry being reauthenticated.

        Wrapped in a method so tests / older HA versions that lack the
        helper can monkeypatch this single accessor.
        """
        return self.hass.config_entries.async_get_entry(self.context["entry_id"])
