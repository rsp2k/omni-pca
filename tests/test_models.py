"""Unit tests for omni_pca.models — payload parsers, no I/O."""

from __future__ import annotations

import pytest

from omni_pca.models import (
    MODEL_NAMES,
    AreaProperties,
    SystemInformation,
    SystemStatus,
    UnitProperties,
    ZoneProperties,
)


def _name_field(name: str, width: int) -> bytes:
    """Pack a name into a fixed-width NUL-padded ASCII field."""
    encoded = name.encode("latin-1")
    if len(encoded) > width:
        raise ValueError("name too long for field")
    return encoded + b"\x00" * (width - len(encoded))


# ---- SystemInformation ----------------------------------------------------


def test_models_system_information_parse() -> None:
    payload = bytes([
        16,    # model byte = OMNI_PRO_II
        2,     # firmware major
        12,    # firmware minor
        1,     # firmware revision (positive => release "rN")
    ]) + _name_field("415-555-1212", 24)

    info = SystemInformation.parse(payload)

    assert info.model_byte == 16
    assert info.model_name == "Omni Pro II"
    assert info.firmware_major == 2
    assert info.firmware_minor == 12
    assert info.firmware_revision == 1
    assert info.firmware_version == "2.12r1"
    assert info.local_phone == "415-555-1212"


def test_models_system_information_beta_revision() -> None:
    """Negative sbyte revision indicates a beta build."""
    payload = bytes([30, 4, 0, 0xFD]) + _name_field("", 24)
    info = SystemInformation.parse(payload)
    assert info.firmware_revision == -3
    assert info.firmware_version == "4.0b3"
    assert info.model_name == "Omni IIe"


def test_models_system_information_unknown_model() -> None:
    payload = bytes([99, 1, 0, 0]) + _name_field("", 24)
    info = SystemInformation.parse(payload)
    assert info.model_name.startswith("Unknown")


def test_models_system_information_short_payload_rejected() -> None:
    with pytest.raises(ValueError):
        SystemInformation.parse(b"\x10\x02")


def test_models_model_name_table_covers_required() -> None:
    for byte, expected_substr in [
        (16, "Omni Pro II"),
        (30, "Omni IIe"),
        (38, "Omni LTe"),
        (36, "Lumina"),
        (37, "Lumina Pro"),
    ]:
        assert MODEL_NAMES[byte] == expected_substr


# ---- SystemStatus ---------------------------------------------------------


def test_models_system_status_parse() -> None:
    # date 2025-12-31 14:30:45, sunrise 06:45, sunset 17:20, battery 0xE0
    payload = bytes([
        1,    # time valid
        25,   # year (offset 2000)
        12,
        31,
        4,    # day-of-week (Wed-ish; ignored in the dataclass)
        14,
        30,
        45,
        0,    # daylight flag
        6,
        45,
        17,
        20,
        0xE0,
    ])
    status = SystemStatus.parse(payload)
    assert status.time_valid is True
    assert status.panel_time is not None
    assert status.panel_time.year == 2025
    assert status.panel_time.month == 12
    assert status.panel_time.day == 31
    assert status.panel_time.hour == 14
    assert status.panel_time.minute == 30
    assert status.panel_time.second == 45
    assert status.sunrise_hour == 6
    assert status.sunset_minute == 20
    assert status.battery_reading == 0xE0
    assert status.battery_ok is True
    assert status.ac_ok is True
    assert status.communication_ok is True
    assert status.troubles == ()


def test_models_system_status_low_battery_flagged() -> None:
    payload = bytes([1, 25, 1, 1, 1, 0, 0, 0, 0, 6, 0, 18, 0, 0x10])
    status = SystemStatus.parse(payload)
    assert status.battery_ok is False
    assert "battery_low" in status.troubles


def test_models_system_status_alarm_pairs_extracted() -> None:
    base = bytes([1, 25, 1, 1, 1, 0, 0, 0, 0, 6, 0, 18, 0, 0xC0])
    alarms_data = bytes([0x01, 0x02, 0x10, 0x20])
    status = SystemStatus.parse(base + alarms_data)
    assert status.area_alarms == ((0x01, 0x02), (0x10, 0x20))


def test_models_system_status_short_payload_rejected() -> None:
    with pytest.raises(ValueError):
        SystemStatus.parse(b"\x00\x00\x00")


# ---- ZoneProperties -------------------------------------------------------


def test_models_zone_properties_parse() -> None:
    # object_type=Zone(1), object_number=42, status=0, loop=0,
    # zone_type=0 (EntryExit), area=1, options=0, name="Front Door"
    payload = (
        bytes([1])           # object type = Zone
        + bytes([0, 42])     # object number = 42 (BE)
        + bytes([0, 0])      # status, loop
        + bytes([0, 1, 0])   # zone type, area, options
        + _name_field("Front Door", 15)
    )
    zone = ZoneProperties.parse(payload)
    assert zone.index == 42
    assert zone.name == "Front Door"
    assert zone.zone_type == 0
    assert zone.area == 1
    assert zone.options == 0


def test_models_zone_properties_wrong_object_type_rejected() -> None:
    payload = bytes([2, 0, 1, 0, 0, 0, 0, 0]) + _name_field("X", 15)
    with pytest.raises(ValueError, match="expected Zone"):
        ZoneProperties.parse(payload)


# ---- UnitProperties -------------------------------------------------------


def test_models_unit_properties_parse() -> None:
    payload = (
        bytes([2])                # Unit
        + bytes([0, 7])           # index 7
        + bytes([0])              # status
        + bytes([0, 0])           # time
        + bytes([1])              # unit_type = Standard
        + _name_field("Lamp", 12)
        + bytes([0])              # gap byte (Data[20] in C# offset)
        + bytes([0x05])           # areas
    )
    unit = UnitProperties.parse(payload)
    assert unit.index == 7
    assert unit.name == "Lamp"
    assert unit.unit_type == 1
    assert unit.areas == 0x05


# ---- AreaProperties -------------------------------------------------------


def test_models_area_properties_parse() -> None:
    payload = (
        bytes([5])              # Area
        + bytes([0, 1])         # index 1
        + bytes([0])            # mode = Off
        + bytes([0])            # alarms
        + bytes([0])            # entry timer
        + bytes([0])            # exit timer
        + bytes([1])            # enabled
        + bytes([60])           # exit delay
        + bytes([30])           # entry delay
        + _name_field("Main", 12)
    )
    area = AreaProperties.parse(payload)
    assert area.index == 1
    assert area.name == "Main"
    assert area.mode == 0
    assert area.enabled is True
    assert area.exit_delay == 60
    assert area.entry_delay == 30
