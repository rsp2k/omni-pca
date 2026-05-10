"""Diagnostics dump for an Omni panel config entry.

Captures a redacted snapshot of the coordinator's data so the user can
attach it to a bug report. Sensitive fields (controller key, PII in
device names) are stripped or hashed.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_PORT

from .const import CONF_CONTROLLER_KEY, DOMAIN
from .coordinator import OmniDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

REDACTED_KEYS = {CONF_CONTROLLER_KEY, "controller_key", "password", "code"}


def _hash_name(name: str) -> str:
    """Hash a panel-defined name so we can confirm uniqueness without leaking it."""
    return "n_" + hashlib.sha256(name.encode("utf-8", errors="ignore")).hexdigest()[:12]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), REDACTED_KEYS),
            CONF_HOST: entry.data.get(CONF_HOST),
            CONF_PORT: entry.data.get(CONF_PORT),
        },
        "panel": (
            {
                "model_byte": data.system_info.model_byte,
                "model_name": data.system_info.model_name,
                "firmware_version": data.system_info.firmware_version,
            }
            if data and data.system_info
            else None
        ),
        "discovered_counts": {
            "zones": len(data.zones) if data else 0,
            "units": len(data.units) if data else 0,
            "areas": len(data.areas) if data else 0,
            "thermostats": len(data.thermostats) if data else 0,
            "buttons": len(data.buttons) if data else 0,
            "programs": len(data.programs) if data else 0,
        },
        "live_status_counts": {
            "zone_status": len(data.zone_status) if data else 0,
            "unit_status": len(data.unit_status) if data else 0,
            "area_status": len(data.area_status) if data else 0,
            "thermostat_status": len(data.thermostat_status) if data else 0,
        },
        "name_hashes": (
            {
                "zones": {idx: _hash_name(props.name) for idx, props in data.zones.items()},
                "units": {idx: _hash_name(props.name) for idx, props in data.units.items()},
                "areas": {idx: _hash_name(props.name) for idx, props in data.areas.items()},
            }
            if data
            else {}
        ),
        "last_event_class": (
            type(data.last_event).__name__ if data and data.last_event else None
        ),
        "last_update_success": coordinator.last_update_success,
        "update_interval_seconds": (
            coordinator.update_interval.total_seconds()
            if coordinator.update_interval
            else None
        ),
    }
