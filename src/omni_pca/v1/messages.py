"""V1 status-reply and name parsers.

The v1 wire protocol's typed status messages (ZoneStatus, UnitStatus,
ThermostatStatus, AuxiliaryStatus) carry one record per object in the
range the client requested — but, unlike v2's ExtendedStatus, the records
do **not** include the object number. The starting index is implicit
from the request payload, and each record is at a fixed offset.

This module supplies "block" parsers that take both the reply payload
and the starting index, and produce a list of the existing top-level
dataclasses (:class:`omni_pca.models.ZoneStatus` etc) so HA entity code
doesn't need a v1-specific schema. The :func:`parse_v1_namedata` helper
decodes the bulk-name-download replies streamed by ``UploadNames``.

Per-record byte counts (verified against firmware 2.12 over UDP):
    ZoneStatus        2 bytes per zone   (status, analog_loop)
    UnitStatus        3 bytes per unit   (status, time_hi, time_lo)
    ThermostatStatus  7 bytes per tstat  (status, current_t, heat_sp,
                                          cool_sp, sys_mode, fan_mode,
                                          hold_mode)
    AuxiliaryStatus   4 bytes per aux    (relay, current, low_sp,
                                          high_sp)

Cross-references (HAI OmniPro II Installation Manual):
    *INSTALLER SETUP → SETUP ZONES* (pca-re/docs/manuals/
        installation_manual/04_INSTALLER_SETUP/) — the zone-type and
        zone-options bits that determine what each ``ZoneStatus.raw_status``
        byte's high nibble means come from this chapter.
    *INSTALLER SETUP → SETUP TEMPERATURES* — same chapter, thermostat
        enable/disable + thermostat type that drives whether
        ``parse_v1_thermostat_status`` records are populated at all.
    *APPENDIX C — ZONE AND UNIT MAPPING* (12_…) — what each record's
        synthesized index *means* on the hardware side (e.g. unit 257+
        = expansion-enclosure outputs, 393+ = panel flags).

References:
    clsOLMsgZoneStatus.cs        / clsOLMsgRequestZoneStatus.cs
    clsOLMsgUnitStatus.cs        / clsOLMsgRequestUnitStatus.cs
    clsOLMsgThermostatStatus.cs  / clsOLMsgRequestThermostatStatus.cs
    clsOLMsgAuxiliaryStatus.cs   / clsOLMsgRequestAuxiliaryStatus.cs
    clsOLMsgSystemStatus.cs      — v1 byte 14 = battery, then per-area Mode
    clsOLMsgNameData.cs          — bulk name download record format
    enuNameType.cs               — Zone=1 Unit=2 Button=3 Code=4 Area=5
                                   Tstat=6 Message=7 UserSetting=8
                                   AccessControlReader=9
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from ..models import (
    AuxSensorStatus,
    SystemStatus,
    ThermostatStatus,
    UnitStatus,
    ZoneStatus,
)

_ZONE_RECORD_BYTES = 2
_UNIT_RECORD_BYTES = 3
_THERMOSTAT_RECORD_BYTES = 7
_AUX_RECORD_BYTES = 4


def parse_v1_zone_status(payload: bytes, start_index: int) -> list[ZoneStatus]:
    """Parse a v1 ZoneStatus reply payload into per-zone dataclasses.

    ``payload`` is the inner Message ``payload`` (data minus opcode byte);
    its length must be a multiple of ``_ZONE_RECORD_BYTES``.
    """
    if len(payload) % _ZONE_RECORD_BYTES != 0:
        raise ValueError(
            f"v1 ZoneStatus payload length {len(payload)} not a multiple of "
            f"{_ZONE_RECORD_BYTES}"
        )
    out: list[ZoneStatus] = []
    for i, off in enumerate(range(0, len(payload), _ZONE_RECORD_BYTES)):
        out.append(
            ZoneStatus(
                index=start_index + i,
                raw_status=payload[off],
                loop=payload[off + 1],
            )
        )
    return out


def parse_v1_unit_status(payload: bytes, start_index: int) -> list[UnitStatus]:
    """Parse a v1 UnitStatus reply payload into per-unit dataclasses."""
    if len(payload) % _UNIT_RECORD_BYTES != 0:
        raise ValueError(
            f"v1 UnitStatus payload length {len(payload)} not a multiple of "
            f"{_UNIT_RECORD_BYTES}"
        )
    out: list[UnitStatus] = []
    for i, off in enumerate(range(0, len(payload), _UNIT_RECORD_BYTES)):
        out.append(
            UnitStatus(
                index=start_index + i,
                state=payload[off],
                time_remaining_secs=(payload[off + 1] << 8) | payload[off + 2],
            )
        )
    return out


def parse_v1_thermostat_status(
    payload: bytes, start_index: int
) -> list[ThermostatStatus]:
    """Parse a v1 ThermostatStatus reply payload into per-tstat dataclasses.

    The v1 record only carries 7 fields; the v2 dataclass has 4 more
    (humidity, humidify_setpoint, dehumidify_setpoint, outdoor_temp,
    horc_status). We zero-fill those — HA's climate platform doesn't
    require them and an explicit 0 is more honest than a fake value.
    """
    if len(payload) % _THERMOSTAT_RECORD_BYTES != 0:
        raise ValueError(
            f"v1 ThermostatStatus payload length {len(payload)} not a multiple "
            f"of {_THERMOSTAT_RECORD_BYTES}"
        )
    out: list[ThermostatStatus] = []
    for i, off in enumerate(range(0, len(payload), _THERMOSTAT_RECORD_BYTES)):
        out.append(
            ThermostatStatus(
                index=start_index + i,
                status=payload[off],
                temperature_raw=payload[off + 1],
                heat_setpoint_raw=payload[off + 2],
                cool_setpoint_raw=payload[off + 3],
                system_mode=payload[off + 4],
                fan_mode=payload[off + 5],
                hold_mode=payload[off + 6],
                humidity_raw=0,
                humidify_setpoint_raw=0,
                dehumidify_setpoint_raw=0,
                outdoor_temperature_raw=0,
                horc_status=0,
            )
        )
    return out


def parse_v1_aux_status(payload: bytes, start_index: int) -> list[AuxSensorStatus]:
    """Parse a v1 AuxiliaryStatus reply payload into per-aux dataclasses."""
    if len(payload) % _AUX_RECORD_BYTES != 0:
        raise ValueError(
            f"v1 AuxiliaryStatus payload length {len(payload)} not a multiple "
            f"of {_AUX_RECORD_BYTES}"
        )
    out: list[AuxSensorStatus] = []
    for i, off in enumerate(range(0, len(payload), _AUX_RECORD_BYTES)):
        out.append(
            AuxSensorStatus(
                index=start_index + i,
                output=payload[off],
                value_raw=payload[off + 1],
                low_raw=payload[off + 2],
                high_raw=payload[off + 3],
            )
        )
    return out


def parse_v1_system_status(payload: bytes) -> SystemStatus:
    """Parse a v1 SystemStatus reply.

    Bytes 0..13 are byte-identical to v2 (time/date + sunrise/sunset +
    battery). After byte 13 v1 carries per-area Mode bytes (1 byte each)
    while v2 carries 2-byte alarm-flag pairs. We translate to the v2
    dataclass's ``area_alarms`` shape by promoting each v1 mode byte to
    a ``(mode, 0)`` tuple — that way HA code that already consumes
    :class:`SystemStatus` keeps working without a v1-specific branch.
    """
    if len(payload) < 14:
        raise ValueError(
            f"v1 SystemStatus payload too short: {len(payload)} bytes"
        )
    time_valid = payload[0] != 0
    year = payload[1]
    month = payload[2]
    day = payload[3]
    # day_of_week = payload[4]
    hour = payload[5]
    minute = payload[6]
    second = payload[7]
    # daylight = payload[8]
    sunrise_h = payload[9]
    sunrise_m = payload[10]
    sunset_h = payload[11]
    sunset_m = payload[12]
    battery = payload[13]

    panel_time: datetime | None = None
    if time_valid:
        try:
            panel_time = datetime(
                year=2000 + year,
                month=month,
                day=day,
                hour=hour,
                minute=minute,
                second=second,
            )
        except ValueError:
            panel_time = None

    # Promote each v1 per-area mode byte to a (mode, 0) pair so the v2
    # area_alarms tuple shape carries the same information without a
    # second dataclass.
    mode_bytes = payload[14:]
    area_alarms = tuple((b, 0) for b in mode_bytes)

    return SystemStatus(
        time_valid=time_valid,
        panel_time=panel_time,
        sunrise_hour=sunrise_h,
        sunrise_minute=sunrise_m,
        sunset_hour=sunset_h,
        sunset_minute=sunset_m,
        battery_reading=battery,
        area_alarms=area_alarms,
    )


# ---- NameData --------------------------------------------------------------


class NameType(IntEnum):
    """Categories of named objects panels can stream over UploadNames.

    Reference: enuNameType.cs.
    """

    ZONE = 1
    UNIT = 2
    BUTTON = 3
    CODE = 4
    AREA = 5
    THERMOSTAT = 6
    MESSAGE = 7
    USER_SETTING = 8
    ACCESS_CONTROL_READER = 9


# Per-type max name length (clsCapOMNI_PRO_II.cs lines 55-71).
# Other Omni models share these numbers — the few exceptions are
# documented but not relevant for the panels we know speak v1+UDP.
_NAME_TYPE_LENGTH: dict[int, int] = {
    NameType.ZONE: 15,
    NameType.UNIT: 12,
    NameType.BUTTON: 12,
    NameType.CODE: 12,
    NameType.AREA: 12,
    NameType.THERMOSTAT: 12,
    NameType.MESSAGE: 15,
    NameType.USER_SETTING: 15,
    NameType.ACCESS_CONTROL_READER: 15,
}


@dataclass(frozen=True, slots=True)
class NameRecord:
    """One name record from a v1 ``NameData`` reply (opcode 11)."""

    name_type: int
    number: int
    name: str

    @property
    def name_type_label(self) -> str:
        try:
            return NameType(self.name_type).name
        except ValueError:
            return f"Unknown({self.name_type})"


def parse_v1_namedata(payload: bytes) -> NameRecord:
    """Decode a v1 ``NameData`` payload (opcode 11) into a :class:`NameRecord`.

    Wire layout (per clsOLMsgNameData.cs, MessageLength is the
    full Data byte count including the opcode):

    * One-byte form (NameNumber ≤ 255), MessageLength = 4 + NameTypeLen:
      ``[opcode][type][num][name×L][\\0]`` — one trailing reserved byte.
    * Two-byte form (NameNumber > 255), MessageLength = 5 + NameTypeLen:
      ``[opcode][type][num_hi][num_lo][name×L][\\0]``.

    ``payload`` here is the *inner* :attr:`Message.payload` (data minus
    the leading opcode), so the lengths to compare against are L+3 and
    L+4 respectively.
    """
    if len(payload) < 3:
        raise ValueError(f"NameData payload too short: {len(payload)} bytes")
    name_type = payload[0]
    name_len = _NAME_TYPE_LENGTH.get(name_type)

    if name_len is not None:
        # Disambiguate by payload length against the expected forms.
        one_byte_len = name_len + 3   # type + num + name + 1 trailing
        two_byte_len = name_len + 4   # type + num_hi + num_lo + name + 1 trailing
        if len(payload) >= two_byte_len:
            number = (payload[1] << 8) | payload[2]
            name_bytes = payload[3 : 3 + name_len]
        elif len(payload) >= one_byte_len:
            number = payload[1]
            name_bytes = payload[2 : 2 + name_len]
        else:
            # Short payload — best-effort one-byte decode of whatever is left.
            number = payload[1]
            name_bytes = payload[2:]
    else:
        # Unknown type — can't tell the form. Assume one-byte and consume
        # the rest; HA filters by known type anyway.
        number = payload[1]
        name_bytes = payload[2:]

    name = name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return NameRecord(name_type=name_type, number=number, name=name)
