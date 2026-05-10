"""Service handlers for the omni_pca integration.

Services give the user a write-surface for things the entity layer
doesn't naturally expose: program execution (no Properties opcode for
Programs in v1.0), arbitrary panel messages, raw commands for power
users, and panel-wide alert acknowledgement.

All services route through the per-entry coordinator's ``OmniClient``;
each accepts an ``entry_id`` field so HA can pick the right panel when
multiple are configured.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.const import ATTR_CONFIG_ENTRY_ID as CONF_ENTRY_ID
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from omni_pca.commands import Command, CommandFailedError

from .const import DOMAIN, LOGGER
from .coordinator import OmniDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, ServiceCall

SERVICE_BYPASS_ZONE = "bypass_zone"
SERVICE_RESTORE_ZONE = "restore_zone"
SERVICE_EXECUTE_PROGRAM = "execute_program"
SERVICE_SHOW_MESSAGE = "show_message"
SERVICE_CLEAR_MESSAGE = "clear_message"
SERVICE_ACKNOWLEDGE_ALERTS = "acknowledge_alerts"
SERVICE_SEND_COMMAND = "send_command"

ATTR_ZONE_INDEX = "zone_index"
ATTR_PROGRAM_INDEX = "program_index"
ATTR_MESSAGE_INDEX = "message_index"
ATTR_COMMAND = "command"
ATTR_PARAM_1 = "parameter1"
ATTR_PARAM_2 = "parameter2"


_BASE_SCHEMA = vol.Schema({vol.Required(CONF_ENTRY_ID): cv.string})


def _zone_schema() -> vol.Schema:
    return _BASE_SCHEMA.extend(
        {vol.Required(ATTR_ZONE_INDEX): vol.All(int, vol.Range(min=1, max=0xFFFF))}
    )


def _program_schema() -> vol.Schema:
    return _BASE_SCHEMA.extend(
        {vol.Required(ATTR_PROGRAM_INDEX): vol.All(int, vol.Range(min=1, max=0xFFFF))}
    )


def _message_schema() -> vol.Schema:
    return _BASE_SCHEMA.extend(
        {vol.Required(ATTR_MESSAGE_INDEX): vol.All(int, vol.Range(min=1, max=0xFFFF))}
    )


def _command_schema() -> vol.Schema:
    return _BASE_SCHEMA.extend(
        {
            vol.Required(ATTR_COMMAND): vol.All(int, vol.Range(min=0, max=255)),
            vol.Optional(ATTR_PARAM_1, default=0): vol.All(
                int, vol.Range(min=0, max=255)
            ),
            vol.Optional(ATTR_PARAM_2, default=0): vol.All(
                int, vol.Range(min=0, max=0xFFFF)
            ),
        }
    )


def _coordinator_for(
    hass: HomeAssistant, call: ServiceCall
) -> OmniDataUpdateCoordinator:
    entry_id = call.data[CONF_ENTRY_ID]
    coordinators = hass.data.get(DOMAIN, {})
    if entry_id not in coordinators:
        raise ServiceValidationError(
            f"No Omni panel configured with entry_id {entry_id!r}"
        )
    return coordinators[entry_id]


async def _wrap(coro_factory) -> None:  # type: ignore[no-untyped-def]
    try:
        await coro_factory()
    except CommandFailedError as err:
        raise HomeAssistantError(f"Panel rejected command: {err}") from err


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register all services for the integration. Idempotent."""

    if hass.services.has_service(DOMAIN, SERVICE_BYPASS_ZONE):
        return  # already registered (multiple entries reuse the same services)

    async def _bypass_zone(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        idx = int(call.data[ATTR_ZONE_INDEX])
        await _wrap(lambda: coord.client.bypass_zone(idx))

    async def _restore_zone(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        idx = int(call.data[ATTR_ZONE_INDEX])
        await _wrap(lambda: coord.client.restore_zone(idx))

    async def _execute_program(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        idx = int(call.data[ATTR_PROGRAM_INDEX])
        await _wrap(lambda: coord.client.execute_program(idx))

    async def _show_message(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        idx = int(call.data[ATTR_MESSAGE_INDEX])
        await _wrap(lambda: coord.client.show_message(idx))

    async def _clear_message(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        idx = int(call.data[ATTR_MESSAGE_INDEX])
        await _wrap(lambda: coord.client.clear_message(idx))

    async def _acknowledge_alerts(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        await _wrap(lambda: coord.client.acknowledge_alerts())

    async def _send_command(call: ServiceCall) -> None:
        coord = _coordinator_for(hass, call)
        cmd_byte = int(call.data[ATTR_COMMAND])
        try:
            cmd = Command(cmd_byte)
        except ValueError as err:
            raise ServiceValidationError(
                f"Unknown Command code {cmd_byte}; see omni_pca.commands.Command"
            ) from err
        p1 = int(call.data[ATTR_PARAM_1])
        p2 = int(call.data[ATTR_PARAM_2])
        LOGGER.debug("send_command %s p1=%d p2=%d", cmd.name, p1, p2)
        await _wrap(lambda: coord.client.execute_command(cmd, p1, p2))

    hass.services.async_register(
        DOMAIN, SERVICE_BYPASS_ZONE, _bypass_zone, schema=_zone_schema()
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTORE_ZONE, _restore_zone, schema=_zone_schema()
    )
    hass.services.async_register(
        DOMAIN, SERVICE_EXECUTE_PROGRAM, _execute_program, schema=_program_schema()
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SHOW_MESSAGE, _show_message, schema=_message_schema()
    )
    hass.services.async_register(
        DOMAIN, SERVICE_CLEAR_MESSAGE, _clear_message, schema=_message_schema()
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ACKNOWLEDGE_ALERTS, _acknowledge_alerts, schema=_BASE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SEND_COMMAND, _send_command, schema=_command_schema()
    )


async def async_unload_services(hass: HomeAssistant) -> None:
    """Tear down services if no entries remain."""
    if hass.data.get(DOMAIN):
        return  # other entries still active
    for svc in (
        SERVICE_BYPASS_ZONE,
        SERVICE_RESTORE_ZONE,
        SERVICE_EXECUTE_PROGRAM,
        SERVICE_SHOW_MESSAGE,
        SERVICE_CLEAR_MESSAGE,
        SERVICE_ACKNOWLEDGE_ALERTS,
        SERVICE_SEND_COMMAND,
    ):
        hass.services.async_remove(DOMAIN, svc)
