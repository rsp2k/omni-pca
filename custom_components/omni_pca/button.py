"""Button platform — one HA button per discovered Omni button macro.

Programs aren't currently discoverable (the library doesn't yet have a
RequestProperties path for the Program object type), so program-execute
support lives in the services platform instead (Phase C).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from omni_pca.commands import CommandFailedError

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator
from .helpers import prettify_name

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
        OmniPanelButton(coordinator, index)
        for index in sorted(coordinator.data.buttons)
    ]
    async_add_entities(entities)


class OmniPanelButton(
    CoordinatorEntity[OmniDataUpdateCoordinator], ButtonEntity
):
    """Push-button entity that fires an Omni button macro."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_unique_id = f"{coordinator.unique_id}-button-{index}"
        props = coordinator.data.buttons[index]
        self._attr_name = prettify_name(props.name) or f"Button {index}"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        try:
            await self.coordinator.client.execute_button(self._index)
        except CommandFailedError as err:
            raise HomeAssistantError(f"Panel rejected button: {err}") from err
