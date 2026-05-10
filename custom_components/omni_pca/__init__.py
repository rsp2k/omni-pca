"""HAI/Leviton Omni Panel integration for Home Assistant."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_CONTROLLER_KEY, DOMAIN, LOGGER
from .coordinator import OmniDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR]


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

    coordinator = OmniDataUpdateCoordinator(
        hass,
        entry,
        host=host,
        port=port,
        controller_key=controller_key,
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # Re-raise so HA retries with backoff; clean up any half-open client.
        await coordinator.async_shutdown()
        raise

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown()
    return unloaded
