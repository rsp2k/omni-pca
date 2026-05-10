"""Typed dataclasses for parsed Omni-Link II v2 reply payloads.

Each class is built from the raw inner-message ``payload`` bytes — i.e.
everything in the ``Message.data`` array AFTER the opcode byte. The
classmethod ``parse(payload)`` does the work; the dataclass itself stays
purely descriptive.

References:
    clsOL2MsgSystemInformation.cs    — model byte + firmware + phone
    clsOL2MsgSystemStatus.cs         — date/time + battery + alarms
    clsOL2MsgProperties.cs           — per-object-type field offsets
    enuModel.cs                      — model byte → human name
    clsUtil.ByteArrayToString        — null-terminated, latin-1, fixed-width
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar, Self

# --------------------------------------------------------------------------
# enuModel byte → human-friendly name. Built from
# decompiled/project/HAI_Shared/enuModel.cs.
# --------------------------------------------------------------------------

MODEL_NAMES: dict[int, str] = {
    0: "Unknown",
    1: "Old Chip v5",
    2: "Omni",
    3: "HAI 2000",
    4: "Omni Pro",
    5: "Aegis 2000",
    6: "HAI 2000 Plus",
    7: "HMS 925",
    8: "HMS 1050",
    9: "Omni LT",
    10: "HMS 800",
    11: "FSN AC",
    12: "Siemens BCM",
    15: "Omni II",
    16: "Omni Pro II",
    17: "HMS 950",
    18: "Aegis 3000",
    19: "HMS 1100",
    20: "Aegis 1000",
    21: "Aegis 1500",
    22: "DOMAIKE D42",
    23: "DOMAIKE D62",
    24: "DOMAIKE D82",
    25: "SC 2000-1",
    26: "SC 2000-2 Plus",
    27: "SC 2000-4",
    28: "Siemens ECM",
    29: "Siemens CCM",
    30: "Omni IIe",
    31: "DOMAIKE D62e",
    32: "HMS 950e",
    33: "SC 2000-2e",
    34: "Aegis 1500e",
    35: "Siemens ECMe",
    36: "Lumina",
    37: "Lumina Pro",
    38: "Omni LTe",
    39: "Omni LTe EU",
    40: "Omni IIe EU",
    41: "Omni Pro II EU",
}


def _decode_name(buf: bytes) -> str:
    """Decode a fixed-width name field as the C# code does (null-terminated, ASCII).

    clsUtil.ByteArrayToString iterates raw bytes and casts each to a
    char, stopping at the first 0 byte. We treat input as latin-1
    (one-byte-one-codepoint) and strip at the first NUL.
    """
    nul = buf.find(b"\x00")
    if nul >= 0:
        buf = buf[:nul]
    return buf.decode("latin-1", errors="replace")


# --------------------------------------------------------------------------
# SystemInformation
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemInformation:
    """Parsed payload of a v2 ``SystemInformation`` (opcode 23) reply.

    Wire layout (clsOL2MsgSystemInformation.cs):
        0       model byte (enuModel)
        1       firmware major
        2       firmware minor
        3       firmware revision (signed; negative = beta)
        4..27   24-byte ASCII local-phone-number, NUL-padded
    """

    model_byte: int
    model_name: str
    firmware_major: int
    firmware_minor: int
    firmware_revision: int
    local_phone: str

    @property
    def firmware_version(self) -> str:
        """Human-friendly version string, e.g. ``"2.12r1"`` or ``"2.12b3"``."""
        rev = self.firmware_revision
        if rev > 0:
            return f"{self.firmware_major}.{self.firmware_minor}r{rev}"
        if rev < 0:
            return f"{self.firmware_major}.{self.firmware_minor}b{-rev}"
        return f"{self.firmware_major}.{self.firmware_minor}"

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 4:
            raise ValueError(
                f"SystemInformation payload too short: {len(payload)} bytes"
            )
        model_byte = payload[0]
        major = payload[1]
        minor = payload[2]
        # Revision is signed (sbyte): negative values mean beta builds.
        rev = payload[3]
        if rev >= 0x80:
            rev -= 0x100
        phone_bytes = payload[4:28] if len(payload) >= 28 else payload[4:]
        return cls(
            model_byte=model_byte,
            model_name=MODEL_NAMES.get(model_byte, f"Unknown ({model_byte})"),
            firmware_major=major,
            firmware_minor=minor,
            firmware_revision=rev,
            local_phone=_decode_name(phone_bytes),
        )


# --------------------------------------------------------------------------
# SystemStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemStatus:
    """Parsed payload of a v2 ``SystemStatus`` (opcode 25) reply.

    Wire layout (clsOL2MsgSystemStatus.cs):
        0       time/date valid flag (0 = not yet set)
        1       year (2-digit, +2000)
        2       month
        3       day
        4       day-of-week (1=Sun..7=Sat)
        5       hour
        6       minute
        7       second
        8       daylight saving flag
        9       sunrise hour
        10      sunrise minute
        11      sunset hour
        12      sunset minute
        13      battery reading (0-255 raw)
        14..N   2 bytes per area alarm flag set
    """

    time_valid: bool
    panel_time: datetime | None
    sunrise_hour: int
    sunrise_minute: int
    sunset_hour: int
    sunset_minute: int
    battery_reading: int
    area_alarms: tuple[tuple[int, int], ...]

    # Convenience flags requested in the spec — derived from
    # ``battery_reading`` and the absence of any alarms / area errors.
    # The wire protocol doesn't expose dedicated AC / comm flags; PC
    # Access infers them from System Troubles. We surface the raw byte
    # and let a higher layer interpret.
    BATTERY_OK_THRESHOLD: ClassVar[int] = 0xC0  # ~75% of 255

    @property
    def battery_ok(self) -> bool:
        return self.battery_reading >= self.BATTERY_OK_THRESHOLD

    @property
    def ac_ok(self) -> bool:
        # Without RequestSystemTroubles we approximate: a battery reading
        # of 0 implies "AC down, battery dead too" or "panel hasn't
        # initialized" — treat both as not-ok.
        return self.battery_reading != 0

    @property
    def communication_ok(self) -> bool:
        # We're talking to the panel right now; if any of this parsed,
        # comms are by definition working at least for this query.
        return True

    @property
    def troubles(self) -> tuple[str, ...]:
        bad: list[str] = []
        if not self.battery_ok:
            bad.append("battery_low")
        if not self.ac_ok:
            bad.append("ac_loss")
        if self.area_alarms:
            bad.append("area_alarm")
        return tuple(bad)

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 14:
            raise ValueError(
                f"SystemStatus payload too short: {len(payload)} bytes"
            )
        time_valid = payload[0] != 0
        year = payload[1]
        month = payload[2]
        day = payload[3]
        # day_of_week = payload[4]   # 1=Sun .. 7=Sat — unused here
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

        # Each area alarm entry is 2 bytes. Pair them up.
        alarm_bytes = payload[14:]
        usable = len(alarm_bytes) - (len(alarm_bytes) % 2)
        alarms = tuple(
            (alarm_bytes[i], alarm_bytes[i + 1]) for i in range(0, usable, 2)
        )
        return cls(
            time_valid=time_valid,
            panel_time=panel_time,
            sunrise_hour=sunrise_h,
            sunrise_minute=sunrise_m,
            sunset_hour=sunset_h,
            sunset_minute=sunset_m,
            battery_reading=battery,
            area_alarms=alarms,
        )


# --------------------------------------------------------------------------
# Properties — common header
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PropertiesHeader:
    object_type: int
    object_number: int

    @classmethod
    def from_payload(cls, payload: bytes) -> Self:
        if len(payload) < 3:
            raise ValueError(
                f"Properties payload too short: {len(payload)} bytes"
            )
        return cls(
            object_type=payload[0],
            object_number=(payload[1] << 8) | payload[2],
        )


# --------------------------------------------------------------------------
# ZoneProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZoneProperties:
    """Parsed Properties (opcode 33) reply for a Zone object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Zone):
        0       object type byte (Zone = 1)
        1..2    object number (BE ushort)
        3       zone status (raw)
        4       zone loop reading
        5       zone type (enuZoneType)
        6       area number
        7       options bitfield
        8..22   15-byte name, NUL-padded
    """

    index: int
    name: str
    zone_type: int
    area: int
    options: int
    status: int
    loop: int

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != 1:
            raise ValueError(
                f"expected Zone (object_type=1), got {hdr.object_type}"
            )
        if len(payload) < 8 + 15:
            raise ValueError(
                f"ZoneProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            status=payload[3],
            loop=payload[4],
            zone_type=payload[5],
            area=payload[6],
            options=payload[7],
            name=_decode_name(payload[8 : 8 + 15]),
        )


# --------------------------------------------------------------------------
# UnitProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitProperties:
    """Parsed Properties (opcode 33) reply for a Unit object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Unit):
        0       object type (Unit = 2)
        1..2    object number (BE ushort)
        3       unit status
        4..5    unit time (BE ushort)
        6       unit type (enuOL2UnitType)
        7..18   12-byte name
        19      unit areas bitfield (Data[21] in the C# class — that's
                Data[1+offset], so payload[20] in zero-based offset, but
                the C# accessor reads Data[21] which corresponds to our
                payload[20] when we strip the opcode byte).
    """

    index: int
    name: str
    unit_type: int
    status: int
    time: int
    areas: int

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != 2:
            raise ValueError(
                f"expected Unit (object_type=2), got {hdr.object_type}"
            )
        if len(payload) < 7 + 12:
            raise ValueError(
                f"UnitProperties payload too short: {len(payload)} bytes"
            )
        # In the C#, Data[0]=opcode, Data[1]=type, Data[2..3]=number,
        # Data[4]=status, Data[5..6]=time, Data[7]=unit_type,
        # Data[8..19]=12-byte name, Data[21]=areas.
        # Our payload[i] == C# Data[i+1], so: status=payload[3],
        # time=payload[4..5], unit_type=payload[6], name=payload[7..18],
        # areas=payload[20].
        areas = payload[20] if len(payload) > 20 else 0
        return cls(
            index=hdr.object_number,
            status=payload[3],
            time=(payload[4] << 8) | payload[5],
            unit_type=payload[6],
            name=_decode_name(payload[7 : 7 + 12]),
            areas=areas,
        )


# --------------------------------------------------------------------------
# AreaProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AreaProperties:
    """Parsed Properties (opcode 33) reply for an Area object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Area):
        payload[0]      object type (Area = 5)
        payload[1..2]   object number
        payload[3]      area mode (enuSecurityMode)
        payload[4]      area alarms bitfield
        payload[5]      entry timer
        payload[6]      exit timer
        payload[7]      enabled flag
        payload[8]      exit delay
        payload[9]      entry delay
        payload[10..21] 12-byte name
    """

    index: int
    name: str
    mode: int
    alarms: int
    enabled: bool
    entry_delay: int
    exit_delay: int

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != 5:
            raise ValueError(
                f"expected Area (object_type=5), got {hdr.object_type}"
            )
        if len(payload) < 10 + 12:
            raise ValueError(
                f"AreaProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            mode=payload[3],
            alarms=payload[4],
            enabled=payload[7] != 0,
            exit_delay=payload[8],
            entry_delay=payload[9],
            name=_decode_name(payload[10 : 10 + 12]),
        )


# --------------------------------------------------------------------------
# Convenience union for callers that don't know the type at compile time
# --------------------------------------------------------------------------

PropertiesReply = ZoneProperties | UnitProperties | AreaProperties
