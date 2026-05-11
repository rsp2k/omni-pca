"""Unit tests for omni_pca.v1.messages parsers.

Test vectors are real wire payloads captured from a firmware-2.12 Omni
Pro II panel via dev/probe_v1_recon.py — see the comment above each
test for the inputs that produced it.
"""

from __future__ import annotations

import pytest

from omni_pca.v1.messages import (
    parse_v1_aux_status,
    parse_v1_system_status,
    parse_v1_thermostat_status,
    parse_v1_unit_status,
    parse_v1_zone_status,
)


# ---- ZoneStatus ---------------------------------------------------------


def test_v1_zone_status_secure_and_open() -> None:
    # Captured: RequestZoneStatus(1, 8) → 16-byte payload, 8 zones × 2 bytes.
    # zone 6 raw_status=0x01 (open), all others 0x00.
    payload = bytes.fromhex("0080007f007f0080008001fd00810080")
    zones = parse_v1_zone_status(payload, start_index=1)
    assert len(zones) == 8
    assert {z.index for z in zones} == set(range(1, 9))
    assert zones[0].is_secure  # zone 1
    assert zones[5].is_open    # zone 6
    assert zones[5].raw_status == 0x01
    assert zones[5].loop == 0xFD


def test_v1_zone_status_indexes_offset_by_start() -> None:
    # If we requested zones 17..24, the same 16-byte payload should
    # produce indexes 17..24.
    payload = bytes.fromhex("0080007f007f0080008001fd00810080")
    zones = parse_v1_zone_status(payload, start_index=17)
    assert {z.index for z in zones} == set(range(17, 25))


def test_v1_zone_status_invalid_length() -> None:
    with pytest.raises(ValueError, match="multiple of 2"):
        parse_v1_zone_status(b"\x00\x00\x00", start_index=1)


# ---- UnitStatus ---------------------------------------------------------


def test_v1_unit_status_dimmer_levels() -> None:
    # Captured: RequestUnitStatus(1, 8) → 24-byte payload, 8 units × 3 bytes.
    # state bytes: 01, 01, 69, 96, 69, 00, 73, 00 → 100%, 100%, 5%, 50%, 5%, 0%, 15%, 0%
    payload = bytes.fromhex("010000010000690000960000690000000000730000000000")
    units = parse_v1_unit_status(payload, start_index=1)
    assert len(units) == 8
    assert units[0].is_on and units[0].brightness == 100  # state=0x01
    assert units[2].brightness == 5    # state=0x69 = 105 → -100 = 5%
    assert units[3].brightness == 50   # state=0x96 = 150 → -100 = 50%
    assert not units[5].is_on          # state=0x00
    assert units[6].brightness == 15   # state=0x73 = 115 → -100 = 15%


def test_v1_unit_status_time_remaining_be_u16() -> None:
    # Single record with remaining=0x1234.
    payload = bytes([0x01, 0x12, 0x34])
    units = parse_v1_unit_status(payload, start_index=42)
    assert len(units) == 1
    assert units[0].index == 42
    assert units[0].time_remaining_secs == 0x1234


def test_v1_unit_status_invalid_length() -> None:
    with pytest.raises(ValueError, match="multiple of 3"):
        parse_v1_unit_status(b"\x00\x00", start_index=1)


# ---- ThermostatStatus ---------------------------------------------------


def test_v1_thermostat_status_unconfigured() -> None:
    # Captured: RequestThermostatStatus(1, 4) → 28 B, all values 0/0/0/0/0/0/0
    # except byte 0 of records 0-1 which is 0x01 (status). The "raw=0" temps
    # decode to -40°C / -40°F per omni_temp_to_fahrenheit.
    payload = bytes.fromhex(
        "01000000000000010000000000000000000000000000000000000000"
    )
    tstats = parse_v1_thermostat_status(payload, start_index=1)
    assert len(tstats) == 4
    assert tstats[0].status == 0x01
    assert tstats[2].status == 0x00
    assert tstats[0].humidity_raw == 0  # zero-filled (v1 doesn't carry it)
    assert tstats[0].outdoor_temperature_raw == 0
    assert tstats[0].horc_status == 0


def test_v1_thermostat_full_record() -> None:
    # Hand-constructed: status=0x01, temp=170 (=45°F), heat=140 (30°F),
    # cool=200 (60°F), mode=1, fan=2, hold=3.
    payload = bytes([0x01, 170, 140, 200, 1, 2, 3])
    tstats = parse_v1_thermostat_status(payload, start_index=5)
    assert len(tstats) == 1
    t = tstats[0]
    assert t.index == 5
    assert t.status == 0x01
    assert t.temperature_raw == 170
    assert t.heat_setpoint_raw == 140
    assert t.cool_setpoint_raw == 200
    assert t.system_mode == 1
    assert t.fan_mode == 2
    assert t.hold_mode == 3


def test_v1_thermostat_invalid_length() -> None:
    with pytest.raises(ValueError, match="multiple of 7"):
        parse_v1_thermostat_status(b"\x00" * 6, start_index=1)


# ---- AuxiliaryStatus ----------------------------------------------------


def test_v1_aux_status_all_zero() -> None:
    # Captured: RequestAuxiliaryStatus(1, 8) → 32 B all zeros.
    payload = bytes(32)
    auxes = parse_v1_aux_status(payload, start_index=1)
    assert len(auxes) == 8
    assert all(a.output == 0 and a.value_raw == 0 for a in auxes)


def test_v1_aux_status_record_field_order() -> None:
    # Single record: output=11, value=22, low=33, high=44
    payload = bytes([11, 22, 33, 44])
    auxes = parse_v1_aux_status(payload, start_index=99)
    assert len(auxes) == 1
    a = auxes[0]
    assert a.index == 99
    assert a.output == 11
    assert a.value_raw == 22
    assert a.low_raw == 33
    assert a.high_raw == 44


def test_v1_aux_invalid_length() -> None:
    with pytest.raises(ValueError, match="multiple of 4"):
        parse_v1_aux_status(b"\x00\x00\x00", start_index=1)


# ---- SystemStatus -------------------------------------------------------


def test_v1_system_status_full_payload() -> None:
    # Captured: RequestSystemStatus → 38 B payload from firmware 2.12.
    # Bytes: 011a050a07163b1c01061c150003 + 24 area-mode bytes
    # decode: time_valid=1, year=26 (=2026), month=05, day=10,
    # dow=07, hour=22, min=59, sec=28, dst=01, sun_h=06, sun_m=28,
    # sun_h2=21, sun_m2=21, battery=0x00, then area modes.
    # Note: the 14th byte (0x03) is the BATTERY reading = 3, not 0.
    payload = bytes.fromhex(
        "011a050a07163b1c01061c150003000000000000000002090000000000000000000000000000"
    )
    s = parse_v1_system_status(payload)
    assert s.time_valid is True
    assert s.panel_time is not None
    assert s.panel_time.year == 2000 + 0x1A  # 2026
    assert s.panel_time.month == 0x05
    assert s.panel_time.day == 0x0A
    assert s.sunrise_hour == 0x06
    assert s.sunrise_minute == 0x1C  # 28
    assert s.sunset_hour == 0x15     # 21
    assert s.sunset_minute == 0x00
    assert s.battery_reading == 0x03
    # 24 trailing bytes promoted to area_alarms tuples (mode_byte, 0).
    assert len(s.area_alarms) == 24
    assert s.area_alarms[0] == (0, 0)
    # Area 9 in this capture had mode=2.
    assert s.area_alarms[8] == (2, 0)


def test_v1_system_status_minimum_payload() -> None:
    # Just the 14 header bytes, no area modes.
    payload = bytes(14)
    s = parse_v1_system_status(payload)
    assert s.time_valid is False
    assert s.panel_time is None
    assert s.battery_reading == 0
    assert s.area_alarms == ()


def test_v1_system_status_too_short_raises() -> None:
    with pytest.raises(ValueError, match="too short"):
        parse_v1_system_status(b"\x00" * 13)


# ---- NameData -----------------------------------------------------------


from omni_pca.v1.messages import NameType, parse_v1_namedata  # noqa: E402


def test_v1_namedata_zone_one_byte_form() -> None:
    # Captured: UploadNames stream → first reply = Zone #1 'GARAGE ENTRY'.
    # Payload 18 B = type(1) + num(1) + name(15) + reserved(1).
    payload = bytes.fromhex("010147415241474520454e54525900000000")
    rec = parse_v1_namedata(payload)
    assert rec.name_type == int(NameType.ZONE)
    assert rec.name_type_label == "ZONE"
    assert rec.number == 1
    assert rec.name == "GARAGE ENTRY"


def test_v1_namedata_unit_one_byte_form() -> None:
    # Hand-crafted: Unit #5 = "GARAGE ENTRY" (12-char name slot, no padding need).
    name = "GARAGE ENTRY"
    payload = (
        bytes([int(NameType.UNIT), 5])
        + name.encode("ascii").ljust(12, b"\x00")
        + b"\x00"  # reserved trailing byte
    )
    rec = parse_v1_namedata(payload)
    assert rec.name_type == int(NameType.UNIT)
    assert rec.number == 5
    assert rec.name == name


def test_v1_namedata_unit_two_byte_form() -> None:
    # Unit #257 = 'Z1-LANDSCAPE' — captured from the real panel after the
    # numbered units rolled over 256.
    payload = (
        bytes([int(NameType.UNIT), 0x01, 0x01])  # type, num_hi=1, num_lo=1
        + b"Z1-LANDSCAPE".ljust(12, b"\x00")     # 12-char name
        + b"\x00"
    )
    rec = parse_v1_namedata(payload)
    assert rec.name_type == int(NameType.UNIT)
    assert rec.number == 257
    assert rec.name == "Z1-LANDSCAPE"


def test_v1_namedata_thermostat() -> None:
    payload = (
        bytes([int(NameType.THERMOSTAT), 1])
        + b"DOWNSTAIRS".ljust(12, b"\x00")
        + b"\x00"
    )
    rec = parse_v1_namedata(payload)
    assert rec.name_type == int(NameType.THERMOSTAT)
    assert rec.number == 1
    assert rec.name == "DOWNSTAIRS"


def test_v1_namedata_strips_trailing_nulls() -> None:
    payload = (
        bytes([int(NameType.ZONE), 9])
        + b"HALL MOTION".ljust(15, b"\x00")
        + b"\x00"
    )
    rec = parse_v1_namedata(payload)
    assert rec.name == "HALL MOTION"  # no embedded nulls in result


def test_v1_namedata_unknown_type_falls_through() -> None:
    # Unknown name type — parser should still return SOMETHING by
    # consuming the rest as the name. HA filters by NameType anyway.
    payload = bytes([99, 7]) + b"WHATEVER\x00\x00"
    rec = parse_v1_namedata(payload)
    assert rec.name_type == 99
    assert rec.name_type_label == "Unknown(99)"
    assert rec.number == 7
    assert rec.name == "WHATEVER"


def test_v1_namedata_short_payload_raises() -> None:
    with pytest.raises(ValueError, match="too short"):
        parse_v1_namedata(b"\x01\x00")
