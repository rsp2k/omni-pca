"""Binary sensor platform: one entity per Omni zone."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


# Best-effort mapping from Omni zone-type byte (enuZoneType) to HA device
# class. Anything not listed falls back to OPENING — a sane default for
# perimeter contacts, which dominate residential installs. We pick this
# explicitly rather than guessing motion vs. door from the name.
#
# Reference: HAI_Shared/enuZoneType.cs (subset).
_ZONE_TYPE_TO_DEVICE_CLASS: dict[int, BinarySensorDeviceClass] = {
    0: BinarySensorDeviceClass.OPENING,  # Perimeter
    1: BinarySensorDeviceClass.OPENING,  # PerimeterEntryExit
    2: BinarySensorDeviceClass.MOTION,  # Interior (typically PIR)
    3: BinarySensorDeviceClass.MOTION,  # InteriorAuto
    4: BinarySensorDeviceClass.SAFETY,  # Tamper
    5: BinarySensorDeviceClass.SMOKE,  # Fire
    6: BinarySensorDeviceClass.SAFETY,  # PoliceEmergency
    7: BinarySensorDeviceClass.SAFETY,  # Duress
    8: BinarySensorDeviceClass.SOUND,  # Auxiliary
    32: BinarySensorDeviceClass.SMOKE,  # Auxiliary fire
    33: BinarySensorDeviceClass.GAS,
    34: BinarySensorDeviceClass.MOISTURE,
    80: BinarySensorDeviceClass.MOTION,  # AwayInterior
    81: BinarySensorDeviceClass.MOTION,  # NightInterior
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one binary_sensor per zone the panel reported."""
    coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        OmniZoneBinarySensor(coordinator, index)
        for index in sorted(coordinator.data.zones)
    ]
    async_add_entities(entities)


class OmniZoneBinarySensor(
    CoordinatorEntity[OmniDataUpdateCoordinator], BinarySensorEntity
):
    """A single zone exposed as a binary_sensor."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: OmniDataUpdateCoordinator, index: int) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"{coordinator.unique_id}-zone-{index}"
        self._attr_device_info = coordinator.device_info
        zone = coordinator.data.zones[index]
        self._attr_name = _prettify(zone.name)
        self._attr_device_class = _ZONE_TYPE_TO_DEVICE_CLASS.get(
            zone.zone_type, BinarySensorDeviceClass.OPENING
        )

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._index in self.coordinator.data.zones
        )

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        zone = data.zones.get(self._index)
        if zone is None:
            return None
        return zone.is_open

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        data = self.coordinator.data
        if data is None:
            return None
        zone = data.zones.get(self._index)
        if zone is None:
            return None
        return {
            "zone_index": zone.index,
            "zone_type": zone.zone_type,
            "area": zone.area,
            "raw_status": zone.status,
            "loop_reading": zone.loop,
        }


def _prettify(name: str) -> str:
    """Convert ``FRONT_DOOR`` → ``Front Door`` for HA-friendly display."""
    return name.replace("_", " ").strip().title()
