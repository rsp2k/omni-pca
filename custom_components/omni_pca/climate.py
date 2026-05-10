"""Climate platform — one HA climate entity per discovered thermostat.

Omni stores temperatures in a linear byte (raw = round((°F + 40) * 10/9)).
HA stays in Fahrenheit because the panel is native there; users with HA
configured for metric will see automatic display conversion downstream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE as ATTR_TEMP
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from omni_pca.commands import CommandFailedError
from omni_pca.models import (
    FanMode as OmniFanMode,
)
from omni_pca.models import (
    HoldMode as OmniHoldMode,
)
from omni_pca.models import (
    HvacMode as OmniHvacMode,
)

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator
from .helpers import (
    fahrenheit_to_omni_raw,
    ha_fan_to_omni,
    ha_hold_to_omni,
    ha_hvac_to_omni,
    omni_fan_to_ha,
    omni_hold_to_ha,
    omni_hvac_to_ha,
    prettify_name,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback


_HVAC_STR_TO_ENUM: dict[str, HVACMode] = {
    "off": HVACMode.OFF,
    "heat": HVACMode.HEAT,
    "cool": HVACMode.COOL,
    "heat_cool": HVACMode.HEAT_COOL,
}

PRESET_NONE = "none"
PRESET_HOLD = "hold"
PRESET_VACATION = "vacation"

FAN_AUTO = "auto"
FAN_ON = "on"
FAN_DIFFUSE = "diffuse"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        OmniThermostatClimate(coordinator, index)
        for index in sorted(coordinator.data.thermostats)
    ]
    async_add_entities(entities)


class OmniThermostatClimate(
    CoordinatorEntity[OmniDataUpdateCoordinator], ClimateEntity
):
    """One discovered thermostat as a HA climate entity."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.FAHRENHEIT
    _attr_target_temperature_step = 1.0
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        | ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_hvac_modes: ClassVar[list[HVACMode]] = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
    ]
    _attr_fan_modes: ClassVar[list[str]] = [FAN_AUTO, FAN_ON, FAN_DIFFUSE]
    _attr_preset_modes: ClassVar[list[str]] = [PRESET_NONE, PRESET_HOLD, PRESET_VACATION]

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"{coordinator.unique_id}-thermostat-{index}"
        props = coordinator.data.thermostats[index]
        self._attr_name = prettify_name(props.name) or f"Thermostat {index}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._index in self.coordinator.data.thermostats
        )

    @property
    def _status(self):  # type: ignore[no-untyped-def]
        return self.coordinator.data.thermostat_status.get(self._index)

    @property
    def current_temperature(self) -> float | None:
        s = self._status
        if s is None or s.temperature_raw == 0:
            return None
        return s.temperature_f

    @property
    def current_humidity(self) -> int | None:
        s = self._status
        if s is None or s.humidity_raw == 0:
            return None
        return int(s.humidity_raw)

    @property
    def hvac_mode(self) -> HVACMode | None:
        s = self._status
        if s is None:
            return None
        return _HVAC_STR_TO_ENUM.get(omni_hvac_to_ha(s.system_mode))

    @property
    def target_temperature(self) -> float | None:
        s = self._status
        if s is None:
            return None
        if s.system_mode == int(OmniHvacMode.HEAT):
            return s.heat_setpoint_f
        if s.system_mode == int(OmniHvacMode.COOL):
            return s.cool_setpoint_f
        return None

    @property
    def target_temperature_high(self) -> float | None:
        s = self._status
        if s is None or s.system_mode != int(OmniHvacMode.AUTO):
            return None
        return s.cool_setpoint_f

    @property
    def target_temperature_low(self) -> float | None:
        s = self._status
        if s is None or s.system_mode != int(OmniHvacMode.AUTO):
            return None
        return s.heat_setpoint_f

    @property
    def fan_mode(self) -> str | None:
        s = self._status
        if s is None:
            return None
        return omni_fan_to_ha(s.fan_mode)

    @property
    def preset_mode(self) -> str | None:
        s = self._status
        if s is None:
            return None
        return omni_hold_to_ha(s.hold_mode)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        s = self._status
        if s is None:
            return None
        return {
            "thermostat_index": self._index,
            "outdoor_temperature_f": (
                s.outdoor_temperature_raw and round(s.outdoor_temperature_f, 1)
            ),
            "humidify_setpoint": s.humidify_setpoint_raw,
            "dehumidify_setpoint": s.dehumidify_setpoint_raw,
        }

    # ---- setters ---------------------------------------------------------

    async def _set(self, coro_factory) -> None:  # type: ignore[no-untyped-def]
        try:
            await coro_factory()
        except CommandFailedError as err:
            raise HomeAssistantError(f"Panel rejected command: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        omni_mode = OmniHvacMode(ha_hvac_to_omni(str(hvac_mode)))
        await self._set(
            lambda: self.coordinator.client.set_thermostat_system_mode(
                self._index, omni_mode
            )
        )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        omni_mode = OmniFanMode(ha_fan_to_omni(fan_mode))
        await self._set(
            lambda: self.coordinator.client.set_thermostat_fan_mode(
                self._index, omni_mode
            )
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        omni_mode = OmniHoldMode(ha_hold_to_omni(preset_mode))
        await self._set(
            lambda: self.coordinator.client.set_thermostat_hold_mode(
                self._index, omni_mode
            )
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        if ATTR_HVAC_MODE in kwargs:
            await self.async_set_hvac_mode(kwargs[ATTR_HVAC_MODE])
        s = self._status
        if s is None:
            raise HomeAssistantError("Thermostat not yet polled")

        if ATTR_TARGET_TEMP_LOW in kwargs:
            await self._set(
                lambda: self.coordinator.client.set_thermostat_heat_setpoint_raw(
                    self._index, fahrenheit_to_omni_raw(kwargs[ATTR_TARGET_TEMP_LOW])
                )
            )
        if ATTR_TARGET_TEMP_HIGH in kwargs:
            await self._set(
                lambda: self.coordinator.client.set_thermostat_cool_setpoint_raw(
                    self._index, fahrenheit_to_omni_raw(kwargs[ATTR_TARGET_TEMP_HIGH])
                )
            )
        if ATTR_TEMP in kwargs:
            target_raw = fahrenheit_to_omni_raw(kwargs[ATTR_TEMP])
            # Single setpoint — choose heat or cool based on current mode.
            if s.system_mode == int(OmniHvacMode.HEAT):
                await self._set(
                    lambda: self.coordinator.client.set_thermostat_heat_setpoint_raw(
                        self._index, target_raw
                    )
                )
            elif s.system_mode == int(OmniHvacMode.COOL):
                await self._set(
                    lambda: self.coordinator.client.set_thermostat_cool_setpoint_raw(
                        self._index, target_raw
                    )
                )
