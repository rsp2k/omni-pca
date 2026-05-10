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


# --------------------------------------------------------------------------
# Alarm panel state translation
# --------------------------------------------------------------------------

# String values matching HA's AlarmControlPanelState enum so this module
# stays importable without Home Assistant in the venv.
ALARM_STATE_DISARMED: Final = "disarmed"
ALARM_STATE_ARMED_HOME: Final = "armed_home"
ALARM_STATE_ARMED_AWAY: Final = "armed_away"
ALARM_STATE_ARMED_NIGHT: Final = "armed_night"
ALARM_STATE_ARMED_VACATION: Final = "armed_vacation"
ALARM_STATE_ARMED_CUSTOM_BYPASS: Final = "armed_custom_bypass"
ALARM_STATE_ARMING: Final = "arming"
ALARM_STATE_PENDING: Final = "pending"
ALARM_STATE_TRIGGERED: Final = "triggered"


# Maps SecurityMode (steady-state values) to HA alarm states. Arming-in-
# progress modes (9..14) get mapped via _ARMING_MODE_TO_FINAL — an arming
# area is always reported as ARMING regardless of the destination mode.
_SECURITY_MODE_TO_ALARM_STATE: dict[int, str] = {
    0: ALARM_STATE_DISARMED,
    1: ALARM_STATE_ARMED_HOME,        # DAY
    2: ALARM_STATE_ARMED_NIGHT,       # NIGHT
    3: ALARM_STATE_ARMED_AWAY,        # AWAY
    4: ALARM_STATE_ARMED_VACATION,    # VACATION
    5: ALARM_STATE_ARMED_CUSTOM_BYPASS,  # DAY_INSTANT
    6: ALARM_STATE_ARMED_NIGHT,       # NIGHT_DELAYED
}

_ARMING_MODES: frozenset[int] = frozenset({9, 10, 11, 12, 13, 14})


def security_mode_to_alarm_state(
    mode: int,
    alarm_active: bool = False,
    entry_timer: int = 0,
    exit_timer: int = 0,
) -> str:
    """Map an Omni SecurityMode to a HA alarm_control_panel state string.

    Priority order:
      1. ``alarm_active`` → triggered
      2. ``entry_timer > 0`` → pending
      3. arming-in-progress modes or ``exit_timer > 0`` → arming
      4. steady-state mapping
    """
    if alarm_active:
        return ALARM_STATE_TRIGGERED
    if entry_timer > 0:
        return ALARM_STATE_PENDING
    if mode in _ARMING_MODES or exit_timer > 0:
        return ALARM_STATE_ARMING
    return _SECURITY_MODE_TO_ALARM_STATE.get(mode, ALARM_STATE_DISARMED)


# Inverse for the four standard arm services HA exposes. Returned ints are
# the SecurityMode values to send via execute_security_command.
ARM_SERVICE_TO_SECURITY_MODE: dict[str, int] = {
    "arm_home": 1,           # DAY
    "arm_away": 3,           # AWAY
    "arm_night": 2,          # NIGHT
    "arm_vacation": 4,       # VACATION
    "arm_custom_bypass": 5,  # DAY_INSTANT
    "disarm": 0,             # OFF
}


# --------------------------------------------------------------------------
# Light brightness conversion (Omni 0..100 ↔ HA 0..255)
# --------------------------------------------------------------------------


def omni_state_to_ha_brightness(state: int) -> int | None:
    """Decode a UnitStatus.state byte into HA brightness (1..255) or None.

    Returns None when the unit is off (state == 0). For state == 1 (plain
    "on", non-dimmable) returns 255. For state in 100..200 returns
    ``round((state - 100) * 255 / 100)`` clamped to 1..255.
    """
    if state == 0:
        return None
    if state == 1:
        return 255
    if 100 <= state <= 200:
        percent = state - 100
        return max(1, min(255, round(percent * 255 / 100)))
    # Scene levels (2..13) and ramping codes (17..25): treat as on, full.
    return 255


def ha_brightness_to_omni_percent(brightness: int) -> int:
    """Convert HA's 1..255 brightness to Omni's 0..100 percent.

    Brightness 0 is invalid here (use turn_off); 1 maps to 1%, 255 to 100%.
    """
    if brightness <= 0:
        return 0
    if brightness >= 255:
        return 100
    return max(1, min(100, round(brightness * 100 / 255)))


# --------------------------------------------------------------------------
# HVAC mode translation
# --------------------------------------------------------------------------

HVAC_MODE_OFF: Final = "off"
HVAC_MODE_HEAT: Final = "heat"
HVAC_MODE_COOL: Final = "cool"
HVAC_MODE_HEAT_COOL: Final = "heat_cool"
HVAC_MODE_AUX_HEAT: Final = "heat"  # HA collapses emergency-heat into heat + preset

_OMNI_HVAC_TO_HA: dict[int, str] = {
    0: HVAC_MODE_OFF,
    1: HVAC_MODE_HEAT,
    2: HVAC_MODE_COOL,
    3: HVAC_MODE_HEAT_COOL,
    4: HVAC_MODE_HEAT,    # EMERGENCY_HEAT — HA treats as heat + a preset
}

_HA_HVAC_TO_OMNI: dict[str, int] = {
    HVAC_MODE_OFF: 0,
    HVAC_MODE_HEAT: 1,
    HVAC_MODE_COOL: 2,
    HVAC_MODE_HEAT_COOL: 3,
}


def omni_hvac_to_ha(mode: int) -> str:
    return _OMNI_HVAC_TO_HA.get(mode, HVAC_MODE_OFF)


def ha_hvac_to_omni(mode: str) -> int:
    return _HA_HVAC_TO_OMNI.get(mode, 0)


_OMNI_FAN_TO_HA: dict[int, str] = {0: "auto", 1: "on", 2: "diffuse"}
_HA_FAN_TO_OMNI: dict[str, int] = {"auto": 0, "on": 1, "diffuse": 2, "cycle": 2}


def omni_fan_to_ha(mode: int) -> str:
    return _OMNI_FAN_TO_HA.get(mode, "auto")


def ha_fan_to_omni(mode: str) -> int:
    return _HA_FAN_TO_OMNI.get(mode, 0)


_OMNI_HOLD_TO_HA: dict[int, str] = {0: "none", 1: "hold", 2: "vacation", 0xFF: "hold"}
_HA_HOLD_TO_OMNI: dict[str, int] = {"none": 0, "hold": 1, "vacation": 2}


def omni_hold_to_ha(mode: int) -> str:
    return _OMNI_HOLD_TO_HA.get(mode, "none")


def ha_hold_to_omni(mode: str) -> int:
    return _HA_HOLD_TO_OMNI.get(mode, 0)


# --------------------------------------------------------------------------
# Temperature: HA °F → Omni raw byte
# --------------------------------------------------------------------------
#
# Omni encodes temperature linearly. Per clsText.cs (DecodeTempRaw):
#     °F = round(raw * 9 / 10) - 40
#     °C = raw / 2 - 40
# Inverse:
#     raw = round((°F + 40) * 10 / 9)


def fahrenheit_to_omni_raw(f: float) -> int:
    """Inverse of omni_temp_to_fahrenheit. Clamps to the valid 0..255 byte."""
    raw = round((f + 40) * 10 / 9)
    return max(0, min(255, raw))


def celsius_to_omni_raw(c: float) -> int:
    """Inverse of omni_temp_to_celsius. Clamps to the valid 0..255 byte."""
    raw = round((c + 40) * 2)
    return max(0, min(255, raw))


# --------------------------------------------------------------------------
# Analog zone → sensor device class
# --------------------------------------------------------------------------

SENSOR_DEVICE_CLASS_TEMPERATURE: Final = "temperature"
SENSOR_DEVICE_CLASS_HUMIDITY: Final = "humidity"
SENSOR_DEVICE_CLASS_POWER: Final = "power"

_ANALOG_ZONE_TYPE_TO_DEVICE_CLASS: dict[int, str] = {
    80: SENSOR_DEVICE_CLASS_POWER,        # ENERGY_SAVER
    81: SENSOR_DEVICE_CLASS_TEMPERATURE,  # OUTDOOR_TEMP
    82: SENSOR_DEVICE_CLASS_TEMPERATURE,  # TEMPERATURE
    83: SENSOR_DEVICE_CLASS_TEMPERATURE,  # TEMP_ALARM
    84: SENSOR_DEVICE_CLASS_HUMIDITY,     # HUMIDITY
}


def analog_zone_device_class(zone_type: int) -> str | None:
    """Return the HA SensorDeviceClass string for an analog zone, or None."""
    return _ANALOG_ZONE_TYPE_TO_DEVICE_CLASS.get(zone_type)


# --------------------------------------------------------------------------
# Event surfacing
# --------------------------------------------------------------------------

# Snake_case event-type strings exposed by the EventEntity.
EVENT_TYPE_ZONE_STATE_CHANGED: Final = "zone_state_changed"
EVENT_TYPE_UNIT_STATE_CHANGED: Final = "unit_state_changed"
EVENT_TYPE_ARMING_CHANGED: Final = "arming_changed"
EVENT_TYPE_ALARM_ACTIVATED: Final = "alarm_activated"
EVENT_TYPE_ALARM_CLEARED: Final = "alarm_cleared"
EVENT_TYPE_AC_LOST: Final = "ac_lost"
EVENT_TYPE_AC_RESTORED: Final = "ac_restored"
EVENT_TYPE_BATTERY_LOW: Final = "battery_low"
EVENT_TYPE_BATTERY_RESTORED: Final = "battery_restored"
EVENT_TYPE_USER_MACRO_BUTTON: Final = "user_macro_button"
EVENT_TYPE_PHONE_LINE_DEAD: Final = "phone_line_dead"
EVENT_TYPE_PHONE_LINE_RESTORED: Final = "phone_line_restored"
EVENT_TYPE_UNKNOWN: Final = "unknown"

EVENT_TYPES: tuple[str, ...] = (
    EVENT_TYPE_ZONE_STATE_CHANGED,
    EVENT_TYPE_UNIT_STATE_CHANGED,
    EVENT_TYPE_ARMING_CHANGED,
    EVENT_TYPE_ALARM_ACTIVATED,
    EVENT_TYPE_ALARM_CLEARED,
    EVENT_TYPE_AC_LOST,
    EVENT_TYPE_AC_RESTORED,
    EVENT_TYPE_BATTERY_LOW,
    EVENT_TYPE_BATTERY_RESTORED,
    EVENT_TYPE_USER_MACRO_BUTTON,
    EVENT_TYPE_PHONE_LINE_DEAD,
    EVENT_TYPE_PHONE_LINE_RESTORED,
    EVENT_TYPE_UNKNOWN,
)


def event_type_for(class_name: str) -> str:
    """Map a SystemEvent subclass name to its snake_case event type."""
    mapping = {
        "ZoneStateChanged": EVENT_TYPE_ZONE_STATE_CHANGED,
        "UnitStateChanged": EVENT_TYPE_UNIT_STATE_CHANGED,
        "ArmingChanged": EVENT_TYPE_ARMING_CHANGED,
        "AlarmActivated": EVENT_TYPE_ALARM_ACTIVATED,
        "AlarmCleared": EVENT_TYPE_ALARM_CLEARED,
        "AcLost": EVENT_TYPE_AC_LOST,
        "AcRestored": EVENT_TYPE_AC_RESTORED,
        "BatteryLow": EVENT_TYPE_BATTERY_LOW,
        "BatteryRestored": EVENT_TYPE_BATTERY_RESTORED,
        "UserMacroButton": EVENT_TYPE_USER_MACRO_BUTTON,
        "PhoneLineDead": EVENT_TYPE_PHONE_LINE_DEAD,
        "PhoneLineRestored": EVENT_TYPE_PHONE_LINE_RESTORED,
    }
    return mapping.get(class_name, EVENT_TYPE_UNKNOWN)
