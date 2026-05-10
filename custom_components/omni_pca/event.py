"""Event platform — surfaces the panel's typed push events as a single
``EventEntity`` per panel. The event_type attribute carries the kind of
event; event_data carries the parsed details. Trigger automations on
``platform: event`` filtering by event_type or event_data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from homeassistant.components.event import EventEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator
from .helpers import EVENT_TYPES, event_type_for

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
    async_add_entities([OmniPanelEvent(coordinator)])


class OmniPanelEvent(
    CoordinatorEntity[OmniDataUpdateCoordinator], EventEntity
):
    """One event entity per panel; relays every push event the coordinator sees."""

    _attr_has_entity_name = True
    _attr_event_types: ClassVar[list[str]] = list(EVENT_TYPES)

    def __init__(self, coordinator: OmniDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-events"
        self._attr_name = "Panel Events"
        self._attr_device_info = coordinator.device_info
        self._last_event_id: int | None = None

    def _handle_coordinator_update(self) -> None:
        ev = self.coordinator.data.last_event
        if ev is None:
            return
        # Only fire when the event reference actually changed; the
        # coordinator may push other state without a new event arriving.
        ev_id = id(ev)
        if ev_id == self._last_event_id:
            return
        self._last_event_id = ev_id

        event_data: dict[str, Any] = {"event_class": type(ev).__name__}
        for key in (
            "zone_index", "unit_index", "area_index", "user_index",
            "new_state", "new_mode", "alarm_type",
        ):
            if hasattr(ev, key):
                event_data[key] = getattr(ev, key)

        self._trigger_event(event_type_for(type(ev).__name__), event_data)
        self.async_write_ha_state()
