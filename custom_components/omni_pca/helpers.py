"""Pure helper functions for the omni_pca integration.

Anything in this module is deliberately decoupled from Home Assistant and
the live OmniClient so it can be unit-tested without either dependency.
The HA-side code (binary_sensor, etc.) imports these and converts the
returned strings to ``BinarySensorDeviceClass`` enum members.
"""

from __future__ import annotations

from typing import Final

# String values that correspond 1:1 to HA's BinarySensorDeviceClass enum
# members. We return strings here (instead of importing the enum) so this
# module stays importable without Home Assistant in the venv.
DEVICE_CLASS_OPENING: Final = "opening"
DEVICE_CLASS_DOOR: Final = "door"
DEVICE_CLASS_WINDOW: Final = "window"
DEVICE_CLASS_MOTION: Final = "motion"
DEVICE_CLASS_SMOKE: Final = "smoke"
DEVICE_CLASS_GAS: Final = "gas"
DEVICE_CLASS_MOISTURE: Final = "moisture"
DEVICE_CLASS_TAMPER: Final = "tamper"
DEVICE_CLASS_SAFETY: Final = "safety"
DEVICE_CLASS_PROBLEM: Final = "problem"
DEVICE_CLASS_SOUND: Final = "sound"
DEVICE_CLASS_HEAT: Final = "heat"
DEVICE_CLASS_COLD: Final = "cold"


# Maps the Omni ``enuZoneType`` byte (see ``omni_pca.models.ZoneType``) to
# a HA ``BinarySensorDeviceClass`` string. The mapping is a judgement
# call — Omni's zone-type taxonomy is finer-grained than HA's binary
# sensor classes, so we collapse a few buckets:
#
#   * Perimeter / entry-exit / latching variants    → opening
#     (most installs use these for door/window contacts)
#   * Interior / night / away interior              → motion (PIRs)
#   * Fire family (FIRE/FIRE_EMERGENCY/FIRE_TAMPER) → smoke
#   * Water / freeze                                → moisture / cold
#   * Gas                                           → gas
#   * Tamper / latching tamper                      → tamper
#   * Panic / police / silent duress / aux-emerg    → safety
#   * Temperature / humidity / aux                  → not a binary sensor
#     (callers should skip — see ``is_binary_zone_type``)
#
# The default for any unmapped value is "opening", which matches the
# dominant residential install (perimeter contact).
_ZONE_TYPE_TO_DEVICE_CLASS: dict[int, str] = {
    # Burglary / contact zones
    0: DEVICE_CLASS_OPENING,   # ENTRY_EXIT
    1: DEVICE_CLASS_OPENING,   # PERIMETER
    4: DEVICE_CLASS_OPENING,   # DOUBLE_ENTRY_DELAY
    5: DEVICE_CLASS_OPENING,   # QUAD_ENTRY_DELAY
    6: DEVICE_CLASS_OPENING,   # LATCHING_PERIMETER
    67: DEVICE_CLASS_OPENING,  # EXIT_TERMINATOR
    # Motion zones
    2: DEVICE_CLASS_MOTION,    # NIGHT_INTERIOR
    3: DEVICE_CLASS_MOTION,    # AWAY_INTERIOR
    7: DEVICE_CLASS_MOTION,    # LATCHING_NIGHT_INTERIOR
    8: DEVICE_CLASS_MOTION,    # LATCHING_AWAY_INTERIOR
    # Panic / duress / police family
    16: DEVICE_CLASS_SAFETY,   # PANIC
    17: DEVICE_CLASS_SAFETY,   # POLICE_EMERGENCY
    18: DEVICE_CLASS_SAFETY,   # SILENT_DURESS
    48: DEVICE_CLASS_SAFETY,   # AUX_EMERGENCY
    # Tamper
    19: DEVICE_CLASS_TAMPER,   # TAMPER
    20: DEVICE_CLASS_TAMPER,   # LATCHING_TAMPER
    56: DEVICE_CLASS_TAMPER,   # FIRE_TAMPER (treat as tamper, not smoke)
    # Fire family
    32: DEVICE_CLASS_SMOKE,    # FIRE
    33: DEVICE_CLASS_SMOKE,    # FIRE_EMERGENCY
    # Other safety / environmental
    34: DEVICE_CLASS_GAS,      # GAS
    49: DEVICE_CLASS_PROBLEM,  # TROUBLE
    54: DEVICE_CLASS_COLD,     # FREEZE
    55: DEVICE_CLASS_MOISTURE,  # WATER
    # Sound / aux
    64: DEVICE_CLASS_SOUND,    # AUXILIARY (loose mapping; use sound)
    65: DEVICE_CLASS_OPENING,  # KEYSWITCH
    66: DEVICE_CLASS_OPENING,  # SHUNT_LOCK
}

# Zone-type bytes that don't map to a binary sensor at all — they're
# numeric readings (temperature, humidity, energy) and should be exposed
# via the sensor platform in Phase B instead. We skip these in
# binary_sensor setup.
_ANALOG_ZONE_TYPES: frozenset[int] = frozenset({
    80,  # ENERGY_SAVER
    81,  # OUTDOOR_TEMP
    82,  # TEMPERATURE
    83,  # TEMP_ALARM
    84,  # HUMIDITY
})


def device_class_for_zone_type(zone_type: int) -> str:
    """Return the HA ``BinarySensorDeviceClass`` value for an Omni zone type.

    Defaults to ``"opening"`` — the most common contact-sensor case — for
    any zone-type byte we don't have an explicit mapping for. Callers
    should check :func:`is_binary_zone_type` first to decide whether the
    zone makes sense as a binary sensor at all.
    """
    return _ZONE_TYPE_TO_DEVICE_CLASS.get(zone_type, DEVICE_CLASS_OPENING)


def is_binary_zone_type(zone_type: int) -> bool:
    """True iff this zone type belongs on the binary_sensor platform.

    Analog/numeric zone types (temperature, humidity, energy savers) are
    sensor-platform candidates, not binary sensors, so we filter them out
    here so the coordinator's discovery doesn't have to know.
    """
    return zone_type not in _ANALOG_ZONE_TYPES


# Zone types whose live ``is_on`` semantics should be derived from the
# *latched* alarm bit (alarm tripped) rather than the current condition
# bit (open/closed). Smoke/fire/gas/water/freeze/panic are latching by
# nature — a smoke detector that flashed for one second still wants to
# read "on" until the user clears the alarm.
_LATCHED_ALARM_ZONE_TYPES: frozenset[int] = frozenset({
    16, 17, 18,            # panic family
    19, 20, 56,            # tamper family
    32, 33,                # fire family
    34,                    # gas
    48,                    # aux emergency
    54, 55,                # freeze, water
})


def use_latched_alarm_for_zone(zone_type: int) -> bool:
    """True if this zone's ``is_on`` should track the latched alarm bit.

    For door/window/motion zones we use the *current condition* bit (live
    open/closed). For latching alarm zones (smoke, water, panic, …) we
    instead use the latched-tripped bit so a brief sensor blip stays
    visible to the user until the alarm is cleared.
    """
    return zone_type in _LATCHED_ALARM_ZONE_TYPES


def prettify_name(name: str) -> str:
    """Convert the panel's ``FRONT_DOOR`` style name into ``Front Door``.

    Returns an empty string unchanged so callers can use truthiness to
    detect "no name configured on this index".
    """
    return name.replace("_", " ").strip().title()
