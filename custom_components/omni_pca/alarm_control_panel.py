"""Alarm control panel platform — one entity per discovered Omni area.

State translation lives in :func:`helpers.security_mode_to_alarm_state`
so it stays unit-testable without Home Assistant. Arm / disarm calls
go through :meth:`OmniClient.execute_security_command` which validates
the user code against the panel; a wrong code surfaces as
:class:`HomeAssistantError`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.alarm_control_panel import (
    AlarmControlPanelEntity,
    AlarmControlPanelEntityFeature,
    AlarmControlPanelState,
    CodeFormat,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from omni_pca.commands import CommandFailedError
from omni_pca.models import SecurityMode

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator
from .helpers import (
    ARM_SERVICE_TO_SECURITY_MODE,
    prettify_name,
    security_mode_to_alarm_state,
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
        OmniAreaAlarmPanel(coordinator, index)
        for index in sorted(coordinator.data.areas)
    ]
    async_add_entities(entities)


_ALARM_STATE_STR_TO_ENUM: dict[str, AlarmControlPanelState] = {
    "disarmed": AlarmControlPanelState.DISARMED,
    "armed_home": AlarmControlPanelState.ARMED_HOME,
    "armed_away": AlarmControlPanelState.ARMED_AWAY,
    "armed_night": AlarmControlPanelState.ARMED_NIGHT,
    "armed_vacation": AlarmControlPanelState.ARMED_VACATION,
    "armed_custom_bypass": AlarmControlPanelState.ARMED_CUSTOM_BYPASS,
    "arming": AlarmControlPanelState.ARMING,
    "pending": AlarmControlPanelState.PENDING,
    "triggered": AlarmControlPanelState.TRIGGERED,
}


class OmniAreaAlarmPanel(
    CoordinatorEntity[OmniDataUpdateCoordinator], AlarmControlPanelEntity
):
    """One discovered area as a HA alarm_control_panel."""

    _attr_has_entity_name = True
    _attr_code_arm_required = True
    _attr_code_format = CodeFormat.NUMBER
    _attr_supported_features = (
        AlarmControlPanelEntityFeature.ARM_HOME
        | AlarmControlPanelEntityFeature.ARM_AWAY
        | AlarmControlPanelEntityFeature.ARM_NIGHT
        | AlarmControlPanelEntityFeature.ARM_VACATION
        | AlarmControlPanelEntityFeature.ARM_CUSTOM_BYPASS
    )

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"{coordinator.unique_id}-area-{index}"
        props = coordinator.data.areas[index]
        self._attr_name = prettify_name(props.name) or f"Area {index}"
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._index in self.coordinator.data.areas
        )

    @property
    def alarm_state(self) -> AlarmControlPanelState | None:
        status = self.coordinator.data.area_status.get(self._index)
        if status is None:
            return None
        state_str = security_mode_to_alarm_state(
            mode=status.mode,
            alarm_active=status.alarm_active,
            entry_timer=status.entry_timer_secs,
            exit_timer=status.exit_timer_secs,
        )
        return _ALARM_STATE_STR_TO_ENUM.get(state_str)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self.coordinator.data.area_status.get(self._index)
        if status is None:
            return None
        return {
            "area_index": self._index,
            "raw_mode": status.mode,
            "raw_mode_name": status.mode_name,
            "entry_timer_secs": status.entry_timer_secs,
            "exit_timer_secs": status.exit_timer_secs,
            "last_user": status.last_user,
            "alarms": status.alarms,
        }

    async def _send(self, mode_name: str, code: str | None) -> None:
        if code is None or not code.isdigit():
            raise HomeAssistantError("A numeric user code is required")
        mode_value = ARM_SERVICE_TO_SECURITY_MODE[mode_name]
        try:
            await self.coordinator.client.execute_security_command(
                area=self._index, mode=SecurityMode(mode_value), code=int(code)
            )
        except CommandFailedError as err:
            raise HomeAssistantError(f"Panel rejected command: {err}") from err
        await self.coordinator.async_request_refresh()

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        await self._send("disarm", code)

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        await self._send("arm_home", code)

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        await self._send("arm_away", code)

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        await self._send("arm_night", code)

    async def async_alarm_arm_vacation(self, code: str | None = None) -> None:
        await self._send("arm_vacation", code)

    async def async_alarm_arm_custom_bypass(self, code: str | None = None) -> None:
        await self._send("arm_custom_bypass", code)
