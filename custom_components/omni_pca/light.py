"""Light platform — one HA light entity per discovered Omni unit.

We expose every unit as a dimmable light. On non-dimmable units the
panel silently ignores the brightness component and just toggles, so
the worst case is a relay that ignores the slider — no harm done.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from omni_pca.commands import CommandFailedError

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator
from .helpers import (
    ha_brightness_to_omni_percent,
    omni_state_to_ha_brightness,
    prettify_name,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        OmniUnitLight(coordinator, index)
        for index in sorted(coordinator.data.units)
    ]
    async_add_entities(entities)


class OmniUnitLight(CoordinatorEntity[OmniDataUpdateCoordinator], LightEntity):
    """One discovered unit as a HA light."""

    _attr_has_entity_name = True
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes: ClassVar[set[ColorMode]] = {ColorMode.BRIGHTNESS}

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"{coordinator.unique_id}-unit-{index}"
        props = coordinator.data.units[index]
        self._attr_name = prettify_name(props.name) or f"Unit {index}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._index in self.coordinator.data.units
        )

    @property
    def is_on(self) -> bool | None:
        status = self.coordinator.data.unit_status.get(self._index)
        if status is None:
            return None
        return status.is_on

    @property
    def brightness(self) -> int | None:
        status = self.coordinator.data.unit_status.get(self._index)
        if status is None:
            return None
        return omni_state_to_ha_brightness(status.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self.coordinator.data.unit_status.get(self._index)
        if status is None:
            return None
        return {
            "unit_index": self._index,
            "raw_state": status.state,
            "time_remaining_secs": status.time_remaining_secs,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            if ATTR_BRIGHTNESS in kwargs:
                percent = ha_brightness_to_omni_percent(int(kwargs[ATTR_BRIGHTNESS]))
                await self.coordinator.client.set_unit_level(self._index, percent)
            else:
                await self.coordinator.client.turn_unit_on(self._index)
        except CommandFailedError as err:
            raise HomeAssistantError(f"Panel rejected command: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await self.coordinator.client.turn_unit_off(self._index)
        except CommandFailedError as err:
            raise HomeAssistantError(f"Panel rejected command: {err}") from err
        await self.coordinator.async_request_refresh()
