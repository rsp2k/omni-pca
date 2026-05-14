"""HAI/Leviton Omni Panel integration for Home Assistant.

Forwards every config entry to the full set of platforms wrapping the
omni-pca library: alarm_control_panel (areas), binary_sensor (zones +
system flags), button (panel button macros), climate (thermostats),
event (typed push events), light (units), sensor (analog zones,
thermostat readings, panel telemetry), switch (zone bypass).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CONTROLLER_KEY,
    CONF_PCA_KEY,
    CONF_PCA_PATH,
    CONF_TRANSPORT,
    DEFAULT_TRANSPORT,
    DOMAIN,
    LOGGER,
)
from .coordinator import OmniDataUpdateCoordinator
from .services import async_setup_services, async_unload_services

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CLIMATE,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """No YAML support; everything is config-flow driven."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Omni panel from a config entry."""
    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    try:
        controller_key = bytes.fromhex(entry.data[CONF_CONTROLLER_KEY])
    except ValueError as err:
        LOGGER.error("stored controller key for %s is corrupt: %s", entry.title, err)
        return False

    transport: str = entry.data.get(CONF_TRANSPORT, DEFAULT_TRANSPORT)
    pca_path: str = entry.data.get(CONF_PCA_PATH, "") or ""
    pca_key: int = entry.data.get(CONF_PCA_KEY, 0)
    coordinator = OmniDataUpdateCoordinator(
        hass,
        entry,
        host=host,
        port=port,
        controller_key=controller_key,
        transport=transport,
        pca_path=pca_path or None,
        pca_key=pca_key,
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # Re-raise so HA retries with backoff; clean up any half-open client
        # *and* the background event task spawned by the first refresh.
        await coordinator.async_shutdown()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry.

    ``coordinator.async_shutdown()`` cancels the long-lived event-listener
    task and closes the ``OmniClient`` socket, so HA's reload doesn't
    leak a background coroutine or a half-open TCP connection.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
        await async_unload_services(hass)
    return unloaded
