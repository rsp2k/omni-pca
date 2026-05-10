"""Unit tests for the extended omni_pca.models classes & helpers.

These cover the second wave of typed dataclasses (ZoneStatus, UnitStatus,
ThermostatStatus, etc.) plus the temperature converters and value enums.
The first-wave dataclasses live in test_models.py.
"""

from __future__ import annotations

import pytest

from omni_pca.models import (
    OBJECT_TYPE_TO_PROPERTIES,
    OBJECT_TYPE_TO_STATUS,
    AreaStatus,
    AudioSourceProperties,
    AudioSourceStatus,
    AudioZoneProperties,
    AudioZoneStatus,
    AuxSensorStatus,
    ButtonProperties,
    CodeProperties,
    FanMode,
    HoldMode,
    HvacMode,
    MessageProperties,
    ObjectType,
    ProgramProperties,
    SecurityMode,
    ThermostatKind,
    ThermostatProperties,
    ThermostatStatus,
    UnitStatus,
    UserSettingKind,
    UserSettingProperties,
    UserSettingStatus,
    ZoneStatus,
    ZoneType,
    omni_temp_to_celsius,
    omni_temp_to_fahrenheit,
)


def _name_field(name: str, width: int) -> bytes:
    encoded = name.encode("latin-1")
    if len(encoded) > width:
        raise ValueError("name too long for field")
    return encoded + b"\x00" * (width - len(encoded))


# ---- Enums ----------------------------------------------------------------


def test_models_security_mode_enum_values() -> None:
    """Pinned values from enuSecurityMode.cs."""
    assert SecurityMode.OFF == 0
    assert SecurityMode.DAY == 1
    assert SecurityMode.NIGHT == 2
    assert SecurityMode.AWAY == 3
    assert SecurityMode.VACATION == 4
    assert SecurityMode.DAY_INSTANT == 5
    assert SecurityMode.NIGHT_DELAYED == 6
    # arming-in-progress family
    assert SecurityMode.ARMING_AWAY == 11
    assert SecurityMode.ARMING_NIGHT_DELAYED == 14


def test_models_hvac_mode_enum_values() -> None:
    assert HvacMode.OFF == 0
    assert HvacMode.HEAT == 1
    assert HvacMode.COOL == 2
    assert HvacMode.AUTO == 3
    assert HvacMode.EMERGENCY_HEAT == 4


def test_models_fan_and_hold_mode_enums() -> None:
    assert FanMode.AUTO == 0
    assert FanMode.ON == 1
    assert FanMode.CYCLE == 2
    assert HoldMode.OFF == 0
    assert HoldMode.HOLD == 1
    assert HoldMode.VACATION == 2
    assert HoldMode.OLD_ON == 0xFF


def test_models_zone_type_enum_subset() -> None:
    assert ZoneType.ENTRY_EXIT == 0
    assert ZoneType.PERIMETER == 1
    assert ZoneType.FIRE == 32
    assert ZoneType.AUXILIARY == 64
    assert ZoneType.HUMIDITY == 84


def test_models_object_type_enum_values() -> None:
    assert ObjectType.ZONE == 1
    assert ObjectType.UNIT == 2
    assert ObjectType.AREA == 5
    assert ObjectType.THERMOSTAT == 6
    assert ObjectType.AUDIO_ZONE == 10
    assert ObjectType.USER_SETTING == 13


def test_models_thermostat_kind_and_user_setting_enums() -> None:
    assert ThermostatKind.NOT_USED == 0
    assert ThermostatKind.AUTO_HEAT_COOL == 1
    assert ThermostatKind.HEAT_ONLY == 3
    assert UserSettingKind.UNUSED == 0
    assert UserSettingKind.TEMPERATURE == 3
    assert UserSettingKind.LEVEL == 8


# ---- Temperature converters -----------------------------------------------


def test_models_omni_temp_to_celsius_kat() -> None:
    """Pinned values from clsText.DecodeTempRaw, Celsius branch."""
    assert omni_temp_to_celsius(0) == -40.0
    assert omni_temp_to_celsius(80) == 0.0
    assert omni_temp_to_celsius(128) == 24.0
    assert omni_temp_to_celsius(140) == 30.0
    assert omni_temp_to_celsius(200) == 60.0


def test_models_omni_temp_to_fahrenheit_kat() -> None:
    """Pinned values from clsText.DecodeTempRaw, Fahrenheit branch.

    The C# rounds with int(raw*9/10 + 0.5) - 40, so we replicate that.
    """
    # raw=44 → int(44*0.9+0.5)-40 = int(40.1)-40 = 40-40 = 0°F
    assert omni_temp_to_fahrenheit(44) == 0.0
    # raw=128 → int(128*0.9+0.5)-40 = int(115.7)-40 = 115-40 = 75°F
    assert omni_temp_to_fahrenheit(128) == 75.0
    # raw=200 → int(200*0.9+0.5)-40 = int(180.5)-40 = 180-40 = 140°F
    assert omni_temp_to_fahrenheit(200) == 140.0
    # raw=0 (sentinel "not available") still goes through linearly
    assert omni_temp_to_fahrenheit(0) == -40.0


# ---- ZoneStatus -----------------------------------------------------------


def test_models_zone_status_secure() -> None:
    payload = bytes([0, 5, 0x00, 128])  # zone 5, all bits clear, loop=128
    z = ZoneStatus.parse(payload)
    assert z.index == 5
    assert z.raw_status == 0x00
    assert z.loop == 128
    assert z.current_state == 0
    assert z.latched_state == 0
    assert z.arming_state == 0
    assert z.is_secure
    assert not z.is_open
    assert not z.is_in_alarm
    assert not z.is_bypassed
    assert not z.is_trouble


def test_models_zone_status_tripped_armed() -> None:
    # current=NotReady(1) + tripped(4) + armed(16) = 0x15
    payload = bytes([0, 1, 0x15, 0])
    z = ZoneStatus.parse(payload)
    assert z.current_state == 1
    assert z.latched_state == 4
    assert z.arming_state == 16
    assert not z.is_secure
    assert z.is_in_alarm
    assert not z.is_bypassed


def test_models_zone_status_bypassed_and_trouble() -> None:
    # bypassed(32) + trouble-now(2) = 0x22
    payload = bytes([0, 2, 0x22, 0])
    z = ZoneStatus.parse(payload)
    assert z.is_bypassed
    assert z.is_trouble
    assert not z.is_secure


def test_models_zone_status_had_trouble_history_bit() -> None:
    # only the "had trouble" history bit (0x40) set
    payload = bytes([0, 7, 0x40, 0])
    z = ZoneStatus.parse(payload)
    assert z.is_trouble
    assert z.is_secure  # current condition still 0
    assert not z.is_in_alarm


def test_models_zone_status_short_payload_rejected() -> None:
    with pytest.raises(ValueError):
        ZoneStatus.parse(b"\x00\x01\x00")


# ---- UnitStatus -----------------------------------------------------------


def test_models_unit_status_off() -> None:
    payload = bytes([0, 3, 0, 0, 0])
    u = UnitStatus.parse(payload)
    assert u.index == 3
    assert u.state == 0
    assert u.time_remaining_secs == 0
    assert not u.is_on
    assert u.brightness == 0


def test_models_unit_status_relay_on() -> None:
    payload = bytes([0, 4, 1, 0x00, 60])  # On, 60s remaining
    u = UnitStatus.parse(payload)
    assert u.is_on
    assert u.brightness == 100  # On with no level → treat as 100%
    assert u.time_remaining_secs == 60


def test_models_unit_status_dimmer_level() -> None:
    # state 175 = 75% brightness
    payload = bytes([0, 12, 175, 0, 0])
    u = UnitStatus.parse(payload)
    assert u.is_on
    assert u.brightness == 75


def test_models_unit_status_scene_no_brightness() -> None:
    # state 5 = Scene C (clsUnit.cs:517-525). brightness undefined.
    payload = bytes([0, 9, 5, 0, 0])
    u = UnitStatus.parse(payload)
    assert u.is_on
    assert u.brightness is None


def test_models_unit_status_short_payload_rejected() -> None:
    with pytest.raises(ValueError):
        UnitStatus.parse(b"\x00\x01\x01")


# ---- AreaStatus -----------------------------------------------------------


def test_models_area_status_armed_away() -> None:
    # area 1, mode=Away(3), no alarms, no timers
    payload = bytes([0, 1, 3, 0x00, 0, 0])
    a = AreaStatus.parse(payload)
    assert a.index == 1
    assert a.mode == 3
    assert a.mode_name == "AWAY"
    assert a.is_armed
    assert not a.alarm_active
    assert a.last_user == 0  # documented: not in wire format


def test_models_area_status_off_with_entry_timer() -> None:
    payload = bytes([0, 2, 0, 0x00, 30, 0])
    a = AreaStatus.parse(payload)
    assert a.mode == 0
    assert a.mode_name == "OFF"
    assert not a.is_armed
    assert a.entry_timer_secs == 30


def test_models_area_status_active_alarm() -> None:
    # mode=Day(1), alarms bitfield non-zero
    payload = bytes([0, 1, 1, 0x04, 0, 0])
    a = AreaStatus.parse(payload)
    assert a.mode == 1
    assert a.alarm_active
    assert a.is_armed


def test_models_area_status_unknown_mode() -> None:
    payload = bytes([0, 1, 99, 0, 0, 0])
    a = AreaStatus.parse(payload)
    assert a.mode_name.startswith("Unknown")


# ---- ThermostatProperties -------------------------------------------------


def test_models_thermostat_properties_parse() -> None:
    payload = (
        bytes([6])               # Thermostat
        + bytes([0, 1])          # index 1
        + bytes([1])             # communicating
        + bytes([0])             # temperature
        + bytes([0])             # heat sp
        + bytes([0])             # cool sp
        + bytes([0])             # mode
        + bytes([0])             # fan
        + bytes([0])             # hold
        + bytes([1])             # type=AutoHeatCool
        + _name_field("Den", 12)
    )
    t = ThermostatProperties.parse(payload)
    assert t.index == 1
    assert t.name == "Den"
    assert t.thermostat_type == 1
    assert t.communicating is True


def test_models_thermostat_properties_wrong_type_rejected() -> None:
    payload = bytes([1] + [0] * 22)
    with pytest.raises(ValueError, match="expected Thermostat"):
        ThermostatProperties.parse(payload)


# ---- ThermostatStatus -----------------------------------------------------


def test_models_thermostat_status_parse() -> None:
    # idx=2, status=1, temp=128 (24°C/75°F), heat=120 (20°C),
    # cool=140 (30°C), mode=Heat, fan=Auto, hold=Off,
    # humidity=130 (33% via F decode),
    # humidify=120, dehumidify=140, outdoor=110, h_or_c=1
    payload = bytes([0, 2, 1, 128, 120, 140, 1, 0, 0, 130, 120, 140, 110, 1])
    t = ThermostatStatus.parse(payload)
    assert t.index == 2
    assert t.temperature_raw == 128
    assert t.temperature_c == 24.0
    assert t.temperature_f == 75.0
    assert t.heat_setpoint_raw == 120
    assert t.heat_setpoint_c == 20.0
    assert t.cool_setpoint_raw == 140
    assert t.cool_setpoint_c == 30.0
    assert t.system_mode == 1
    assert t.fan_mode == 0
    assert t.hold_mode == 0
    assert t.humidity_raw == 130
    assert t.outdoor_temperature_raw == 110
    assert t.horc_status == 1


def test_models_thermostat_status_short_payload_rejected() -> None:
    with pytest.raises(ValueError):
        ThermostatStatus.parse(b"\x00\x02\x01")


# ---- ButtonProperties -----------------------------------------------------


def test_models_button_properties_parse() -> None:
    payload = bytes([3, 0, 4]) + _name_field("Welcome", 12)
    b = ButtonProperties.parse(payload)
    assert b.index == 4
    assert b.name == "Welcome"


def test_models_button_properties_wrong_type_rejected() -> None:
    with pytest.raises(ValueError, match="expected Button"):
        ButtonProperties.parse(bytes([1, 0, 1]) + _name_field("X", 12))


# ---- CodeProperties -------------------------------------------------------


def test_models_code_properties_parse_no_digits_exposed() -> None:
    payload = bytes([4, 0, 2]) + _name_field("Alice", 12)
    c = CodeProperties.parse(payload)
    assert c.index == 2
    assert c.name == "Alice"
    # Belt-and-suspenders: the dataclass really has only these two fields
    # of interest. There is no "digits" / "code" attribute.
    assert not hasattr(c, "digits")
    assert not hasattr(c, "code")


# ---- MessageProperties ----------------------------------------------------


def test_models_message_properties_parse() -> None:
    payload = (
        bytes([7, 0, 1])
        + _name_field("Hello world!!", 15)
        + bytes([0])  # gap (Data[19] in C# offset)
        + bytes([0xFF])  # area-group bitfield
    )
    m = MessageProperties.parse(payload)
    assert m.index == 1
    assert m.name == "Hello world!!"
    assert m.text == m.name  # OL2 v1 quirk: name doubles as text
    assert m.areas == 0xFF


# ---- ProgramProperties ----------------------------------------------------


def test_models_program_properties_parse_with_remark() -> None:
    body = bytes(range(14))  # 14 bytes
    payload = bytes([0, 7]) + body + b"sunset blink\x00"
    p = ProgramProperties.parse(payload)
    assert p.index == 7
    assert p.raw_body == body
    assert p.name == "sunset blink"


def test_models_program_properties_parse_no_remark() -> None:
    body = b"\x00" * 14
    payload = bytes([0, 1]) + body
    p = ProgramProperties.parse(payload)
    assert p.index == 1
    assert p.name == ""
    assert len(p.raw_body) == 14


# ---- AuxSensorStatus ------------------------------------------------------


def test_models_aux_sensor_status_parse() -> None:
    # idx=4, output=0, value=140 (30°C), low=120 (20°C), high=160 (40°C)
    payload = bytes([0, 4, 0, 140, 120, 160])
    a = AuxSensorStatus.parse(payload)
    assert a.index == 4
    assert a.value_raw == 140
    assert a.temperature_c == 30.0
    assert a.low_c == 20.0
    assert a.high_c == 40.0


# ---- AudioZone ------------------------------------------------------------


def test_models_audio_zone_properties_parse() -> None:
    payload = (
        bytes([10, 0, 1])           # AudioZone, idx 1
        + bytes([1])                # power on
        + bytes([2])                # source 2
        + bytes([60])               # volume 60
        + bytes([0])                # mute off
        + _name_field("Living", 12)
    )
    az = AudioZoneProperties.parse(payload)
    assert az.index == 1
    assert az.name == "Living"
    assert az.power is True
    assert az.source == 2
    assert az.volume == 60
    assert az.mute is False


def test_models_audio_zone_status_parse() -> None:
    # idx=3, power on, source 5, vol 80, mute on
    payload = bytes([0, 3, 1, 5, 80, 1])
    s = AudioZoneStatus.parse(payload)
    assert s.index == 3
    assert s.power is True
    assert s.source == 5
    assert s.volume == 80
    assert s.mute is True


# ---- AudioSource ----------------------------------------------------------


def test_models_audio_source_properties_parse() -> None:
    payload = bytes([9, 0, 2]) + _name_field("Spotify", 12)
    s = AudioSourceProperties.parse(payload)
    assert s.index == 2
    assert s.name == "Spotify"


def test_models_audio_source_status_parse() -> None:
    # idx=1, seq=42, position=1, field=2, text="Now Playing"
    payload = bytes([0, 1, 42, 1, 2]) + b"Now Playing\x00rest"
    s = AudioSourceStatus.parse(payload)
    assert s.index == 1
    assert s.sequence == 42
    assert s.position == 1
    assert s.field_id == 2
    assert s.text == "Now Playing"


# ---- UserSetting ----------------------------------------------------------


def test_models_user_setting_properties_parse() -> None:
    # type=Temperature(3), value=140 (= 30°C)
    payload = bytes([13, 0, 5, 3, 0, 140]) + _name_field("HotPoint", 15)
    s = UserSettingProperties.parse(payload)
    assert s.index == 5
    assert s.name == "HotPoint"
    assert s.setting_type == 3
    assert s.value == 140


def test_models_user_setting_status_parse() -> None:
    payload = bytes([0, 5, 3, 0, 140])
    s = UserSettingStatus.parse(payload)
    assert s.index == 5
    assert s.setting_type == 3
    assert s.value == 140


# ---- Dispatch tables ------------------------------------------------------


def test_models_object_type_to_properties_dispatch() -> None:
    assert OBJECT_TYPE_TO_PROPERTIES[ObjectType.ZONE].__name__ == "ZoneProperties"
    assert OBJECT_TYPE_TO_PROPERTIES[ObjectType.UNIT].__name__ == "UnitProperties"
    assert (
        OBJECT_TYPE_TO_PROPERTIES[ObjectType.THERMOSTAT].__name__
        == "ThermostatProperties"
    )
    assert OBJECT_TYPE_TO_PROPERTIES[ObjectType.MESSAGE].__name__ == "MessageProperties"


def test_models_object_type_to_status_dispatch() -> None:
    assert OBJECT_TYPE_TO_STATUS[ObjectType.ZONE].__name__ == "ZoneStatus"
    assert OBJECT_TYPE_TO_STATUS[ObjectType.AUXILIARY].__name__ == "AuxSensorStatus"
    assert OBJECT_TYPE_TO_STATUS[ObjectType.AUDIO_ZONE].__name__ == "AudioZoneStatus"


def test_models_dispatch_round_trip_zone() -> None:
    """A caller that only knows the object_type byte can still parse
    a Properties payload through the dispatch table."""
    payload = (
        bytes([1, 0, 42, 0, 0, 0, 1, 0])
        + _name_field("Front Door", 15)
    )
    parser = OBJECT_TYPE_TO_PROPERTIES[payload[0]]
    parsed = parser.parse(payload)
    assert parsed.index == 42
    assert parsed.name == "Front Door"
