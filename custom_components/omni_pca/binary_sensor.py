"""Binary sensor platform for the omni_pca integration.

Per-zone entities
-----------------
* :class:`OmniZoneBinarySensor` — one per discovered zone. ``is_on``
  derives from :class:`~omni_pca.models.ZoneStatus`. The HA device class
  is picked from the zone-type byte by
  :func:`~custom_components.omni_pca.helpers.device_class_for_zone_type`.
* :class:`OmniZoneBypassedBinarySensor` — one per discovered zone.
  Diagnostic entity (``problem`` device-class) that turns on when the
  zone is currently bypassed by the user or auto-bypassed by the panel.

Panel-level entities
--------------------
* :class:`OmniSystemAcBinarySensor` — ``power``-class. ``is_on`` = AC OK.
  Tracks both the periodic SystemStatus poll and any pushed
  :class:`~omni_pca.events.AcLost` / :class:`~omni_pca.events.AcRestored`
  events so HA reacts immediately on a power-blip.
* :class:`OmniSystemBatteryBinarySensor` — ``battery``-class. ``is_on``
  when the backup battery reading drops below the panel's threshold
  (or a :class:`~omni_pca.events.BatteryLow` event came in since the
  last :class:`~omni_pca.events.BatteryRestored`).
* :class:`OmniSystemTroubleBinarySensor` — ``problem``-class. ``is_on``
  when SystemStatus reports any troubles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from omni_pca.events import (
    AcLost,
    AcRestored,
    BatteryLow,
    BatteryRestored,
)

from .const import DOMAIN
from .coordinator import OmniDataUpdateCoordinator
from .helpers import (
    device_class_for_zone_type,
    is_binary_zone_type,
    prettify_name,
    use_latched_alarm_for_zone,
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
    """Create one binary_sensor per discovered zone, plus system-level entities."""
    coordinator: OmniDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []

    for index in sorted(coordinator.data.zones):
        props = coordinator.data.zones[index]
        if not is_binary_zone_type(props.zone_type):
            # Analog zones (temperature, humidity) aren't binary sensors;
            # Phase B will surface them on the sensor platform.
            continue
        entities.append(OmniZoneBinarySensor(coordinator, index))
        entities.append(OmniZoneBypassedBinarySensor(coordinator, index))

    entities.append(OmniSystemAcBinarySensor(coordinator))
    entities.append(OmniSystemBatteryBinarySensor(coordinator))
    entities.append(OmniSystemTroubleBinarySensor(coordinator))

    async_add_entities(entities)


# --------------------------------------------------------------------------
# Zone entities
# --------------------------------------------------------------------------


class _OmniZoneBaseEntity(
    CoordinatorEntity[OmniDataUpdateCoordinator], BinarySensorEntity
):
    """Shared boilerplate for the two per-zone entities."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator)
        self._index = index
        self._attr_device_info = coordinator.device_info

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data is not None
            and self._index in self.coordinator.data.zones
        )

    @property
    def _zone_props(self):  # type: ignore[no-untyped-def]
        return self.coordinator.data.zones.get(self._index)

    @property
    def _zone_status(self):  # type: ignore[no-untyped-def]
        return self.coordinator.data.zone_status.get(self._index)


class OmniZoneBinarySensor(_OmniZoneBaseEntity):
    """A single zone exposed as the primary binary_sensor.

    Live ``is_on`` derives from the matching :class:`ZoneStatus`:

    * For motion / smoke / water / freeze / panic / tamper zones we use
      the *latched* tripped bit so a brief pulse stays visible until the
      user clears the alarm
      (see :func:`~custom_components.omni_pca.helpers.use_latched_alarm_for_zone`).
    * For door / window / opening zones we use the *current condition*
      bit so HA tracks the door truthfully.
    """

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.unique_id}-zone-{index}"
        props = coordinator.data.zones[index]
        self._attr_name = prettify_name(props.name) or f"Zone {index}"
        self._attr_device_class = BinarySensorDeviceClass(
            device_class_for_zone_type(props.zone_type)
        )

    @property
    def is_on(self) -> bool | None:
        status = self._zone_status
        props = self._zone_props
        if status is None or props is None:
            return None
        # Pick the right bit based on zone type — latched-alarm zones
        # (smoke, water, panic, …) stay "on" until cleared even after a
        # one-shot trip, while contact / motion zones track the live
        # current condition bit.
        if use_latched_alarm_for_zone(props.zone_type):
            return status.is_in_alarm
        return status.is_open

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = self._zone_status
        props = self._zone_props
        if status is None or props is None:
            return None
        return {
            "zone_index": self._index,
            "zone_type": props.zone_type,
            "area": props.area,
            "is_open": status.is_open,
            "is_bypassed": status.is_bypassed,
            "is_in_alarm": status.is_in_alarm,
            "is_trouble": status.is_trouble,
            "loop_reading": status.loop,
            "raw_status": status.raw_status,
        }


class OmniZoneBypassedBinarySensor(_OmniZoneBaseEntity):
    """Diagnostic entity that turns on when a zone is bypassed.

    Surfacing bypass as its own entity (rather than just an attribute on
    the primary sensor) lets automations key on it directly — e.g.
    "remind me at 10pm if any zone is still bypassed".
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(
        self, coordinator: OmniDataUpdateCoordinator, index: int
    ) -> None:
        super().__init__(coordinator, index)
        self._attr_unique_id = f"{coordinator.unique_id}-zone-{index}-bypassed"
        props = coordinator.data.zones[index]
        base = prettify_name(props.name) or f"Zone {index}"
        self._attr_name = f"{base} Bypassed"

    @property
    def is_on(self) -> bool | None:
        status = self._zone_status
        if status is None:
            return None
        return status.is_bypassed


# --------------------------------------------------------------------------
# System-level entities
# --------------------------------------------------------------------------


class _OmniSystemBaseEntity(
    CoordinatorEntity[OmniDataUpdateCoordinator], BinarySensorEntity
):
    """Shared boilerplate for hub-scoped system binary sensors."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: OmniDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_device_info = coordinator.device_info


class OmniSystemAcBinarySensor(_OmniSystemBaseEntity):
    """``power`` device class — on when mains AC is present.

    Uses the most recent :class:`AcLost` / :class:`AcRestored` push event
    as the authoritative signal, falling back to the SystemStatus battery
    heuristic when no event has been seen yet (panel never lost AC).
    """

    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator: OmniDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-system-ac"
        self._attr_name = "AC Power"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        last = data.last_event
        if isinstance(last, AcLost):
            return False
        if isinstance(last, AcRestored):
            return True
        if data.system_status is not None:
            return data.system_status.ac_ok
        return None


class OmniSystemBatteryBinarySensor(_OmniSystemBaseEntity):
    """``battery`` device class — on when the backup battery is LOW."""

    _attr_device_class = BinarySensorDeviceClass.BATTERY

    def __init__(self, coordinator: OmniDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-system-battery"
        self._attr_name = "Backup Battery"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None:
            return None
        last = data.last_event
        if isinstance(last, BatteryLow):
            return True
        if isinstance(last, BatteryRestored):
            return False
        if data.system_status is not None:
            return not data.system_status.battery_ok
        return None

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        if self.coordinator.data is None or self.coordinator.data.system_status is None:
            return None
        return {
            "battery_reading": self.coordinator.data.system_status.battery_reading,
        }


class OmniSystemTroubleBinarySensor(_OmniSystemBaseEntity):
    """``problem`` device class — on when SystemStatus reports any troubles."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator: OmniDataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.unique_id}-system-trouble"
        self._attr_name = "System Trouble"

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or data.system_status is None:
            return None
        return bool(data.system_status.troubles)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.coordinator.data.system_status is None:
            return None
        return {
            "troubles": list(self.coordinator.data.system_status.troubles),
        }
