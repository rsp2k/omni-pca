"""Typed dataclasses for parsed Omni-Link II v2 reply payloads.

Each class is built from the raw inner-message ``payload`` bytes — i.e.
everything in the ``Message.data`` array AFTER the opcode byte. The
classmethod ``parse(payload)`` does the work; the dataclass itself stays
purely descriptive.

References:
    clsOL2MsgSystemInformation.cs    — model byte + firmware + phone
    clsOL2MsgSystemStatus.cs         — date/time + battery + alarms
    clsOL2MsgProperties.cs           — per-object-type field offsets
    clsOL2MsgExtendedStatus.cs       — per-object-type live status
    clsOL2MsgAudioSourceStatus.cs    — audio source metadata stream
    clsZone.cs / clsUnit.cs / clsArea.cs / clsThermostat.cs / clsButton.cs /
    clsCode.cs / clsMessage.cs / clsAudioZone.cs / clsAudioSource.cs /
    clsUserSetting.cs / clsProgram.cs                — domain object semantics
    enuObjectType.cs / enuSecurityMode.cs / enuThermostatMode.cs /
    enuThermostatFanMode.cs / enuThermostatHoldMode.cs / enuZoneType.cs /
    enuZoneCurrentCondition.cs / enuZoneArmingStatus.cs /
    enuZoneLachedAlarmStatus.cs / enuUserSettingType.cs /
    enuThermostatType.cs            — value enums
    enuModel.cs                      — model byte → human name
    clsUtil.ByteArrayToString        — null-terminated, latin-1, fixed-width
    clsText.DecodeTempRaw            — Omni temperature byte → °F/°C
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
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
# Object type constants and value enums
# --------------------------------------------------------------------------


class ObjectType(IntEnum):
    """Object-type byte values (enuObjectType.cs).

    These are the same byte that prefixes every Properties / ExtendedStatus
    payload — i.e. ``payload[0]`` for a Properties reply and ``payload[0]``
    for an ExtendedStatus reply (after the opcode is stripped).
    """

    INVALID = 0
    ZONE = 1
    UNIT = 2
    BUTTON = 3
    CODE = 4
    AREA = 5
    THERMOSTAT = 6
    MESSAGE = 7
    AUXILIARY = 8
    AUDIO_SOURCE = 9
    AUDIO_ZONE = 10
    EXPANSION = 11
    CONSOLE = 12
    USER_SETTING = 13
    ACCESS_CONTROL_READER = 14
    ACCESS_CONTROL_LOCK = 15


class SecurityMode(IntEnum):
    """Area security mode (enuSecurityMode.cs).

    Values 9..14 are the "arming in progress" variants the panel reports
    while a delayed-arm timer is running.
    """

    OFF = 0
    DAY = 1
    NIGHT = 2
    AWAY = 3
    VACATION = 4
    DAY_INSTANT = 5
    NIGHT_DELAYED = 6
    ANY_CHANGE = 7
    ARMING_DAY = 9
    ARMING_NIGHT = 10
    ARMING_AWAY = 11
    ARMING_VACATION = 12
    ARMING_DAY_INSTANT = 13
    ARMING_NIGHT_DELAYED = 14


class HvacMode(IntEnum):
    """Thermostat system mode (enuThermostatMode.cs)."""

    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO = 3
    EMERGENCY_HEAT = 4


class FanMode(IntEnum):
    """Thermostat fan mode (enuThermostatFanMode.cs)."""

    AUTO = 0
    ON = 1
    CYCLE = 2


class HoldMode(IntEnum):
    """Thermostat hold mode (enuThermostatHoldMode.cs).

    Value 255 (``OLD_ON``) is a legacy "Hold" sentinel from older firmware
    that some panels still emit; treat it as equivalent to ``HOLD``.
    """

    OFF = 0
    HOLD = 1
    VACATION = 2
    OLD_ON = 0xFF


class ThermostatKind(IntEnum):
    """Thermostat hardware classification (enuThermostatType.cs)."""

    NOT_USED = 0
    AUTO_HEAT_COOL = 1
    HEAT_COOL = 2
    HEAT_ONLY = 3
    COOL_ONLY = 4
    SETPOINT_ONLY = 5


class ZoneType(IntEnum):
    """Zone type (enuZoneType.cs) — common subset.

    The full enum has ~30 entries (extended-range temperature sensors,
    DSC-specific types, etc.); we surface the security-relevant ones plus
    the temperature/humidity sensors and a handful of utility types. Any
    raw byte value still round-trips through ``ZoneStatus.zone_type`` —
    it just won't have a named enum member.
    """

    ENTRY_EXIT = 0
    PERIMETER = 1
    NIGHT_INTERIOR = 2
    AWAY_INTERIOR = 3
    DOUBLE_ENTRY_DELAY = 4
    QUAD_ENTRY_DELAY = 5
    LATCHING_PERIMETER = 6
    LATCHING_NIGHT_INTERIOR = 7
    LATCHING_AWAY_INTERIOR = 8
    PANIC = 16
    POLICE_EMERGENCY = 17
    SILENT_DURESS = 18
    TAMPER = 19
    LATCHING_TAMPER = 20
    FIRE = 32
    FIRE_EMERGENCY = 33
    GAS = 34
    AUX_EMERGENCY = 48
    TROUBLE = 49
    FREEZE = 54
    WATER = 55
    FIRE_TAMPER = 56
    AUXILIARY = 64
    KEYSWITCH = 65
    SHUNT_LOCK = 66
    EXIT_TERMINATOR = 67
    ENERGY_SAVER = 80
    OUTDOOR_TEMP = 81
    TEMPERATURE = 82
    TEMP_ALARM = 83
    HUMIDITY = 84


class UserSettingKind(IntEnum):
    """User-setting value type (enuUserSettingType.cs)."""

    UNUSED = 0
    NUMBER = 1
    DURATION = 2
    TEMPERATURE = 3
    HUMIDITY = 4
    DATE = 5
    TIME = 6
    DAYS_OF_WEEK = 7
    LEVEL = 8


# --------------------------------------------------------------------------
# Temperature conversions
# --------------------------------------------------------------------------


def omni_temp_to_celsius(raw: int) -> float:
    """Convert Omni's raw temperature byte to °C.

    The panel uses a single linear scale: ``°C = raw / 2 - 40``. Domain
    runs from raw=1 (-39.5 °C) through raw=200 (60 °C); raw=0 means "not
    available / unknown" and raw>200 is reserved for User-Setting
    references — this helper still returns the linear value so callers
    can decide how to handle sentinels.

    Reference: clsText.cs:301 (DecodeTempRaw, Celsius branch).
    """
    return raw / 2.0 - 40.0


def omni_temp_to_fahrenheit(raw: int) -> float:
    """Convert Omni's raw temperature byte to °F.

    The C# code rounds to whole degrees:
    ``°F = int(raw * 9 / 10 + 0.5) - 40``. We keep the ``int(...+0.5)``
    rounding to match what PC Access shows on screen — callers that want
    the underlying continuous value can derive it from the Celsius
    helper instead (``°C * 9/5 + 32``).

    Reference: clsText.cs:301-308 (DecodeTempRaw, Fahrenheit branch).
    """
    return float(int(raw * 9.0 / 10.0 + 0.5) - 40)


# --------------------------------------------------------------------------
# ZoneStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ZoneStatus:
    """Live state of a single zone, decoded from one record of an
    ExtendedStatus (opcode 35) reply or a per-zone Status reply.

    Wire layout (each record, ObjectType=Zone, ObjectLength=4):
        bytes[0..1]  zone number (BE u16)
        bytes[2]     status byte (current+latched+arming, see below)
        bytes[3]     analog loop reading (0-255)

    Status byte bit layout (clsZone.cs:385, clsText.cs:3110):
        bits 0-1 (mask 0x03): current condition
            0=Secure, 1=NotReady, 2=Trouble, 3=Tamper
        bits 2-3 (mask 0x0C): latched alarm status
            0=Secure, 4=Tripped, 8=Reset
        bits 4-5 (mask 0x30): arming status
            0=Disarmed, 16=Armed, 32=Bypassed, 48=AutoBypassed
        bit  6   (mask 0x40): "had trouble" history bit

    Reference: clsOL2MsgExtendedStatus.cs:282-307, clsZone.cs:385-414.
    """

    index: int
    raw_status: int
    loop: int

    # Sub-field views derived from raw_status. We keep them as ints so
    # the caller can pattern-match against ``enuZoneCurrentCondition``,
    # ``enuZoneLachedAlarmStatus``, and ``enuZoneArmingStatus`` from the
    # decompiled C# without any conversion at our boundary.

    @property
    def current_state(self) -> int:
        """Low 2 bits — Secure/NotReady/Trouble/Tamper."""
        return self.raw_status & 0x03

    @property
    def latched_state(self) -> int:
        """Mid 2 bits as a raw value (0/4/8) — Secure/Tripped/Reset."""
        return self.raw_status & 0x0C

    @property
    def arming_state(self) -> int:
        """Upper 2 bits as a raw value (0/16/32/48) — Disarmed/Armed/Bypassed/AutoBypassed."""
        return self.raw_status & 0x30

    @property
    def is_secure(self) -> bool:
        """True iff current condition is Secure (low 2 bits == 0)."""
        return self.current_state == 0

    @property
    def is_open(self) -> bool:
        """Convenience: opposite of ``is_secure`` (door/window open or sensor active)."""
        return not self.is_secure

    @property
    def is_in_alarm(self) -> bool:
        """True if the zone is currently tripped (latched bit 0x04 set)."""
        return (self.raw_status & 0x04) == 0x04

    @property
    def is_bypassed(self) -> bool:
        """True for either user-bypassed or auto-bypassed (bits 0x20/0x30)."""
        return (self.raw_status & 0x20) == 0x20 or (
            self.raw_status & 0x30
        ) == 0x30

    @property
    def is_trouble(self) -> bool:
        """True if current condition is Trouble or Tamper, OR the
        "had trouble" history bit (0x40) is set."""
        return (self.raw_status & 0x02) == 0x02 or (
            self.raw_status & 0x40
        ) == 0x40

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 4:
            raise ValueError(
                f"ZoneStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            raw_status=payload[2],
            loop=payload[3],
        )


# --------------------------------------------------------------------------
# UnitStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitStatus:
    """Live state of a single unit (light/output), one record of an
    ExtendedStatus (opcode 35) reply or a per-unit Status reply.

    Wire layout (each record, ObjectType=Unit, ObjectLength≥5):
        bytes[0..1]  unit number (BE u16)
        bytes[2]     state byte (see decoding below)
        bytes[3..4]  remaining time in seconds (BE u16, 0 = indefinite)
        bytes[5..6]  optional ZigBee instantaneous power (W, BE u16)

    State byte semantics (clsUnit.cs:405-533):
        0           Off
        1           On
        2..13       Scene A..L (state - 63 → 'A'..'L' as ASCII char)
        17..25      Dim 1..9 (state - 16)
        26          Blink
        33..41      Brighten 1..9 (state - 32)
        100..200    Brightness level percentage (state - 100, range 0-100)

    Reference: clsOL2MsgExtendedStatus.cs:35-73, clsUnit.cs:405-533.
    """

    index: int
    state: int
    time_remaining_secs: int

    @property
    def is_on(self) -> bool:
        """Anything other than the explicit Off state (0) counts as on."""
        return self.state != 0

    @property
    def brightness(self) -> int | None:
        """Percentage 0-100 if the state byte encodes an absolute level;
        otherwise ``None`` (relays, scenes, ramping, blink)."""
        if 100 <= self.state <= 200:
            return self.state - 100
        if self.state == 0:
            return 0
        if self.state == 1:
            # On with no level info → treat as 100% so callers don't have
            # to special-case relays vs. dimmers when the panel only
            # reports On.
            return 100
        return None

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 5:
            raise ValueError(
                f"UnitStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            state=payload[2],
            time_remaining_secs=(payload[3] << 8) | payload[4],
        )


# --------------------------------------------------------------------------
# AreaStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AreaStatus:
    """Live arming state of a single area, one record of an
    ExtendedStatus (opcode 35) reply.

    Wire layout (each record, ObjectType=Area, ObjectLength=6):
        bytes[0..1]  area number (BE u16)
        bytes[2]     security mode (enuSecurityMode)
        bytes[3]     area alarms bitfield
        bytes[4]     entry timer remaining (seconds)
        bytes[5]     exit timer remaining (seconds)

    The Omni-Link II ExtendedStatus reply does NOT carry "last user" —
    that field is exposed only via the EventLog opcode. We keep
    ``last_user`` in the dataclass for API parity with the spec; it
    defaults to 0 and stays 0 here.

    Reference: clsOL2MsgExtendedStatus.cs:75-118.
    """

    index: int
    mode: int
    last_user: int
    entry_timer_secs: int
    exit_timer_secs: int
    alarms: int

    @property
    def mode_name(self) -> str:
        """Human-friendly mode label from ``SecurityMode`` (or ``"Unknown(N)"``)."""
        try:
            return SecurityMode(self.mode).name
        except ValueError:
            return f"Unknown({self.mode})"

    @property
    def is_armed(self) -> bool:
        """True for any mode other than OFF and ANY_CHANGE.

        ``ANY_CHANGE`` (7) is a programming-condition wildcard, not a
        real arming state, so we treat it as not-armed for status
        purposes.
        """
        return self.mode not in (
            SecurityMode.OFF,
            SecurityMode.ANY_CHANGE,
        )

    @property
    def alarm_active(self) -> bool:
        """True if any alarm bit in the bitfield is set."""
        return self.alarms != 0

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 6:
            raise ValueError(
                f"AreaStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            mode=payload[2],
            alarms=payload[3],
            entry_timer_secs=payload[4],
            exit_timer_secs=payload[5],
            last_user=0,
        )


# --------------------------------------------------------------------------
# ThermostatProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThermostatProperties:
    """Parsed Properties (opcode 33) reply for a Thermostat object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Thermostat):
        payload[0]      object type (Thermostat = 6)
        payload[1..2]   object number (BE u16)
        payload[3]      communicating flag (1 = thermostat is talking to panel)
        payload[4]      temperature (raw)
        payload[5]      heat setpoint (raw)
        payload[6]      cool setpoint (raw)
        payload[7]      mode (enuThermostatMode)
        payload[8]      fan mode (enuThermostatFanMode)
        payload[9]      hold mode (enuThermostatHoldMode)
        payload[10]     thermostat type (enuThermostatType)
        payload[11..22] 12-byte name, NUL-padded

    Mapping note: the C# accessors index ``Data[N]`` where ``Data[0]``
    is the opcode byte. Our ``payload`` strips that opcode, so
    ``payload[i] == Data[i+1]``. That's why the type byte sits at
    ``payload[10]`` (Data[11]) and the name at ``payload[11..22]``
    (Data[12..23]).

    Reference: clsOL2MsgProperties.cs:287-393, 694, clsThermostat.cs.
    """

    index: int
    name: str
    thermostat_type: int
    communicating: bool

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.THERMOSTAT:
            raise ValueError(
                f"expected Thermostat (object_type=6), got {hdr.object_type}"
            )
        if len(payload) < 11 + 12:
            raise ValueError(
                f"ThermostatProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            communicating=payload[3] != 0,
            thermostat_type=payload[10],
            name=_decode_name(payload[11 : 11 + 12]),
        )


# --------------------------------------------------------------------------
# ThermostatStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ThermostatStatus:
    """Live state of a single thermostat from one record of an
    ExtendedStatus reply.

    Wire layout (each record, ObjectType=Thermostat, ObjectLength=14):
        bytes[0..1]  thermostat number (BE u16)
        bytes[2]     communicating/status flag
        bytes[3]     current temperature (raw)
        bytes[4]     heat setpoint (raw)
        bytes[5]     cool setpoint (raw)
        bytes[6]     system mode (enuThermostatMode)
        bytes[7]     fan mode (enuThermostatFanMode)
        bytes[8]     hold mode (enuThermostatHoldMode)
        bytes[9]     humidity (raw, Fahrenheit-scale even on °C panels)
        bytes[10]    humidify setpoint (raw)
        bytes[11]    dehumidify setpoint (raw)
        bytes[12]    outdoor temperature (raw)
        bytes[13]    H or C status (1=heating, 2=cooling — model-dependent)

    All ``*_raw`` values are bytes on Omni's combined temperature scale
    (``°C = raw/2 - 40``); we expose ``*_f`` and ``*_c`` properties that
    apply the scale for the common cases.

    Reference: clsOL2MsgExtendedStatus.cs:120-235.
    """

    index: int
    status: int
    temperature_raw: int
    heat_setpoint_raw: int
    cool_setpoint_raw: int
    system_mode: int
    fan_mode: int
    hold_mode: int
    humidity_raw: int
    humidify_setpoint_raw: int
    dehumidify_setpoint_raw: int
    outdoor_temperature_raw: int
    horc_status: int

    @property
    def temperature_c(self) -> float:
        return omni_temp_to_celsius(self.temperature_raw)

    @property
    def temperature_f(self) -> float:
        return omni_temp_to_fahrenheit(self.temperature_raw)

    @property
    def heat_setpoint_c(self) -> float:
        return omni_temp_to_celsius(self.heat_setpoint_raw)

    @property
    def heat_setpoint_f(self) -> float:
        return omni_temp_to_fahrenheit(self.heat_setpoint_raw)

    @property
    def cool_setpoint_c(self) -> float:
        return omni_temp_to_celsius(self.cool_setpoint_raw)

    @property
    def cool_setpoint_f(self) -> float:
        return omni_temp_to_fahrenheit(self.cool_setpoint_raw)

    @property
    def outdoor_temperature_c(self) -> float:
        return omni_temp_to_celsius(self.outdoor_temperature_raw)

    @property
    def outdoor_temperature_f(self) -> float:
        return omni_temp_to_fahrenheit(self.outdoor_temperature_raw)

    @property
    def humidity_percent(self) -> float:
        """Relative humidity as percentage (0-100).

        The panel stores humidity on the same DecodeTemp scale but
        always interpreted as Fahrenheit, where the 0..100% range
        roughly maps to bytes 89..200 (``F = raw*9/10 + 0.5 - 40``,
        clamped 0-100 by the firmware).
        """
        return omni_temp_to_fahrenheit(self.humidity_raw)

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 14:
            raise ValueError(
                f"ThermostatStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            status=payload[2],
            temperature_raw=payload[3],
            heat_setpoint_raw=payload[4],
            cool_setpoint_raw=payload[5],
            system_mode=payload[6],
            fan_mode=payload[7],
            hold_mode=payload[8],
            humidity_raw=payload[9],
            humidify_setpoint_raw=payload[10],
            dehumidify_setpoint_raw=payload[11],
            outdoor_temperature_raw=payload[12],
            horc_status=payload[13],
        )


# --------------------------------------------------------------------------
# ButtonProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ButtonProperties:
    """Parsed Properties (opcode 33) reply for a Button object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Button):
        payload[0]      object type (Button = 3)
        payload[1..2]   object number (BE u16)
        payload[3..14]  12-byte name, NUL-padded

    Buttons carry no state of their own (you push them, panel runs the
    associated program); only the index + name are exposed here.

    Reference: clsOL2MsgProperties.cs:691, clsButton.cs.
    """

    index: int
    name: str

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.BUTTON:
            raise ValueError(
                f"expected Button (object_type=3), got {hdr.object_type}"
            )
        if len(payload) < 3 + 12:
            raise ValueError(
                f"ButtonProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            name=_decode_name(payload[3 : 3 + 12]),
        )


# --------------------------------------------------------------------------
# ProgramProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProgramProperties:
    """Parsed program record.

    Programs are not exposed via the standard Properties (opcode 33)
    object-type table — clsOL2MsgProperties.ObjectName has no Program
    branch, and the panel returns program data through its own
    request/reply pair (clsOL2MsgRequestProgramData / Msg2Program in
    clsProgram.cs:540). We model what that reply looks like once
    deserialised: an index, a name (only meaningful when the program is
    a Remark), and the raw 14-byte program body so callers can decode
    the conditional/command/schedule fields with help from the
    ``clsProgram.cs`` getter accessors.

    Wire layout assumed (after the opcode byte is stripped):
        payload[0..1]   program number (BE u16)
        payload[2..15]  14-byte raw program body (clsProgram.ToByteArray)
        payload[16..]   optional NUL-terminated remark text

    AMBIGUITY: there is no canonical OL2 Properties opcode for programs,
    and clsProgram has no name field of its own — RemarkText is stored
    in a separate dictionary keyed by RemarkID. We follow the layout
    that the on-disk .pca file uses (number + body + optional remark).

    Reference: clsProgram.cs:564-585 (ToByteArray), clsProgram.cs:301-323
    (RemarkText).
    """

    index: int
    name: str
    raw_body: bytes

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 2 + 14:
            raise ValueError(
                f"ProgramProperties payload too short: {len(payload)} bytes"
            )
        index = (payload[0] << 8) | payload[1]
        body = bytes(payload[2 : 2 + 14])
        remark = _decode_name(payload[2 + 14 :]) if len(payload) > 16 else ""
        return cls(index=index, name=remark, raw_body=body)


# --------------------------------------------------------------------------
# CodeProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CodeProperties:
    """Parsed Properties (opcode 33) reply for a Code object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Code):
        payload[0]      object type (Code = 4)
        payload[1..2]   object number (BE u16)
        payload[3..14]  12-byte name, NUL-padded

    NOTE: The actual digit value of the user code is stored on the panel
    (clsCode.Code) but the Properties reply only carries the name. Even
    if a future firmware were to embed the digits, this dataclass would
    deliberately not expose them — printing real PINs through the model
    layer is a security-by-design no.

    Reference: clsOL2MsgProperties.cs:692, clsCode.cs.
    """

    index: int
    name: str

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.CODE:
            raise ValueError(
                f"expected Code (object_type=4), got {hdr.object_type}"
            )
        if len(payload) < 3 + 12:
            raise ValueError(
                f"CodeProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            name=_decode_name(payload[3 : 3 + 12]),
        )


# --------------------------------------------------------------------------
# MessageProperties
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MessageProperties:
    """Parsed Properties (opcode 33) reply for a Message object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=Message):
        payload[0]      object type (Message = 7)
        payload[1..2]   object number (BE u16)
        payload[3..17]  15-byte name (also used as the message text on
                        text-display models), NUL-padded
        payload[19]     area-group bitfield (Data[20] in C# offset)

    Omni's Message objects double as the "name" and the "text"; longer
    free-form messages are not part of the v2 properties exchange.

    Reference: clsOL2MsgProperties.cs:455-465, 695, clsMessage.cs.
    """

    index: int
    name: str
    text: str
    areas: int

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.MESSAGE:
            raise ValueError(
                f"expected Message (object_type=7), got {hdr.object_type}"
            )
        if len(payload) < 3 + 15:
            raise ValueError(
                f"MessageProperties payload too short: {len(payload)} bytes"
            )
        name = _decode_name(payload[3 : 3 + 15])
        areas = payload[19] if len(payload) > 19 else 0
        # text == name in OL2; preserved as a distinct field so a future
        # extended message reply (clsOLMsgMessageStatus) can populate
        # them independently without breaking callers.
        return cls(index=hdr.object_number, name=name, text=name, areas=areas)


# --------------------------------------------------------------------------
# AuxSensorStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuxSensorStatus:
    """Live state of an auxiliary sensor (one record of an
    ExtendedStatus reply, ObjectType=Auxiliary).

    Wire layout (each record, ObjectType=Auxillary, ObjectLength=6):
        bytes[0..1]  aux sensor number (BE u16)
        bytes[2]     output state byte
        bytes[3]     current temperature/humidity (raw)
        bytes[4]     low setpoint (raw)
        bytes[5]     high setpoint (raw)

    The C# wraps these as a special kind of "zone" (clsZone.Output/High/
    Low/Temp), but the wire reply has its own ObjectType=8 layout. The
    raw byte uses Omni's standard temperature scale for temperature
    sensors and the Fahrenheit-only scale for humidity sensors.

    Reference: clsOL2MsgExtendedStatus.cs:237-280, clsZone.cs:79-93.
    """

    index: int
    output: int
    value_raw: int
    low_raw: int
    high_raw: int

    @property
    def temperature_c(self) -> float:
        return omni_temp_to_celsius(self.value_raw)

    @property
    def temperature_f(self) -> float:
        return omni_temp_to_fahrenheit(self.value_raw)

    @property
    def low_c(self) -> float:
        return omni_temp_to_celsius(self.low_raw)

    @property
    def high_c(self) -> float:
        return omni_temp_to_celsius(self.high_raw)

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 6:
            raise ValueError(
                f"AuxSensorStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            output=payload[2],
            value_raw=payload[3],
            low_raw=payload[4],
            high_raw=payload[5],
        )


# --------------------------------------------------------------------------
# AudioZoneProperties / AudioZoneStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioZoneProperties:
    """Parsed Properties (opcode 33) reply for an AudioZone object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=AudioZone):
        payload[0]      object type (AudioZone = 10)
        payload[1..2]   object number (BE u16)
        payload[3]      power on/off (0 = off)
        payload[4]      currently selected source
        payload[5]      volume (0-100)
        payload[6]      mute (0 = un-muted)
        payload[7..18]  12-byte name, NUL-padded

    Reference: clsOL2MsgProperties.cs:527-580, 698, clsAudioZone.cs.
    """

    index: int
    name: str
    power: bool
    source: int
    volume: int
    mute: bool

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.AUDIO_ZONE:
            raise ValueError(
                f"expected AudioZone (object_type=10), got {hdr.object_type}"
            )
        if len(payload) < 7 + 12:
            raise ValueError(
                f"AudioZoneProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            power=payload[3] != 0,
            source=payload[4],
            volume=payload[5],
            mute=payload[6] != 0,
            name=_decode_name(payload[7 : 7 + 12]),
        )


@dataclass(frozen=True, slots=True)
class AudioZoneStatus:
    """Live state of one audio zone, one record of an ExtendedStatus reply.

    Wire layout (each record, ObjectType=AudioZone, ObjectLength=6):
        bytes[0..1]  zone number (BE u16)
        bytes[2]     power on/off (0 = off)
        bytes[3]     selected source
        bytes[4]     volume (0-100)
        bytes[5]     mute (0 = un-muted)

    Reference: clsOL2MsgExtendedStatus.cs:309-360.
    """

    index: int
    power: bool
    source: int
    volume: int
    mute: bool

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 6:
            raise ValueError(
                f"AudioZoneStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            power=payload[2] != 0,
            source=payload[3],
            volume=payload[4],
            mute=payload[5] != 0,
        )


# --------------------------------------------------------------------------
# AudioSourceProperties / AudioSourceStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AudioSourceProperties:
    """Parsed Properties (opcode 33) reply for an AudioSource object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=AudioSource):
        payload[0]      object type (AudioSource = 9)
        payload[1..2]   object number (BE u16)
        payload[3..14]  12-byte name, NUL-padded

    Reference: clsOL2MsgProperties.cs:697, clsAudioSource.cs.
    """

    index: int
    name: str

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.AUDIO_SOURCE:
            raise ValueError(
                f"expected AudioSource (object_type=9), got {hdr.object_type}"
            )
        if len(payload) < 3 + 12:
            raise ValueError(
                f"AudioSourceProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            name=_decode_name(payload[3 : 3 + 12]),
        )


@dataclass(frozen=True, slots=True)
class AudioSourceStatus:
    """Parsed AudioSourceStatus (opcode-specific) reply.

    Wire layout (clsOL2MsgAudioSourceStatus.cs):
        payload[0..1]   source number (BE u16)
        payload[2]      sequence number (lets clients detect duplicates)
        payload[3]      position (which metadata field this reply is)
        payload[4]      field id (track / artist / album / time / etc.)
        payload[5..]    metadata text, ASCII-ish, NUL-terminated

    Reference: clsOL2MsgAudioSourceStatus.cs.
    """

    index: int
    sequence: int
    position: int
    field_id: int
    text: str

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 5:
            raise ValueError(
                f"AudioSourceStatus payload too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            sequence=payload[2],
            position=payload[3],
            field_id=payload[4],
            text=_decode_name(payload[5:]),
        )


# --------------------------------------------------------------------------
# UserSettingProperties / UserSettingStatus
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserSettingProperties:
    """Parsed Properties (opcode 33) reply for a UserSetting object.

    Wire layout (clsOL2MsgProperties.cs, ObjectType=UserSetting):
        payload[0]      object type (UserSetting = 13)
        payload[1..2]   object number (BE u16)
        payload[3]      setting type (enuUserSettingType)
        payload[4..5]   raw value (BE u16, interpretation depends on type)
        payload[6..20]  15-byte name, NUL-padded

    Reference: clsOL2MsgProperties.cs:583-605, 699, clsUserSetting.cs.
    """

    index: int
    name: str
    setting_type: int
    value: int

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        hdr = _PropertiesHeader.from_payload(payload)
        if hdr.object_type != ObjectType.USER_SETTING:
            raise ValueError(
                f"expected UserSetting (object_type=13), got {hdr.object_type}"
            )
        if len(payload) < 6 + 15:
            raise ValueError(
                f"UserSettingProperties payload too short: {len(payload)} bytes"
            )
        return cls(
            index=hdr.object_number,
            setting_type=payload[3],
            value=(payload[4] << 8) | payload[5],
            name=_decode_name(payload[6 : 6 + 15]),
        )


@dataclass(frozen=True, slots=True)
class UserSettingStatus:
    """Live value of one user setting, one record of an ExtendedStatus reply.

    Wire layout (each record, ObjectType=UserSetting, ObjectLength=5):
        bytes[0..1]  setting number (BE u16)
        bytes[2]     setting type (enuUserSettingType)
        bytes[3..4]  raw value (BE u16)

    Reference: clsOL2MsgExtendedStatus.cs:389-414.
    """

    index: int
    setting_type: int
    value: int

    @classmethod
    def parse(cls, payload: bytes) -> Self:
        if len(payload) < 5:
            raise ValueError(
                f"UserSettingStatus record too short: {len(payload)} bytes"
            )
        return cls(
            index=(payload[0] << 8) | payload[1],
            setting_type=payload[2],
            value=(payload[3] << 8) | payload[4],
        )


# --------------------------------------------------------------------------
# Object-type → parser dispatch tables
# --------------------------------------------------------------------------

OBJECT_TYPE_TO_PROPERTIES: dict[int, type] = {
    ObjectType.ZONE: ZoneProperties,
    ObjectType.UNIT: UnitProperties,
    ObjectType.BUTTON: ButtonProperties,
    ObjectType.CODE: CodeProperties,
    ObjectType.AREA: AreaProperties,
    ObjectType.THERMOSTAT: ThermostatProperties,
    ObjectType.MESSAGE: MessageProperties,
    ObjectType.AUDIO_SOURCE: AudioSourceProperties,
    ObjectType.AUDIO_ZONE: AudioZoneProperties,
    ObjectType.USER_SETTING: UserSettingProperties,
}

OBJECT_TYPE_TO_STATUS: dict[int, type] = {
    ObjectType.ZONE: ZoneStatus,
    ObjectType.UNIT: UnitStatus,
    ObjectType.AREA: AreaStatus,
    ObjectType.THERMOSTAT: ThermostatStatus,
    ObjectType.AUXILIARY: AuxSensorStatus,
    ObjectType.AUDIO_ZONE: AudioZoneStatus,
    ObjectType.AUDIO_SOURCE: AudioSourceStatus,
    ObjectType.USER_SETTING: UserSettingStatus,
}


# --------------------------------------------------------------------------
# Convenience union for callers that don't know the type at compile time
# --------------------------------------------------------------------------

PropertiesReply = (
    ZoneProperties
    | UnitProperties
    | AreaProperties
    | ThermostatProperties
    | ButtonProperties
    | CodeProperties
    | MessageProperties
    | AudioZoneProperties
    | AudioSourceProperties
    | UserSettingProperties
    | ProgramProperties
)

StatusReply = (
    ZoneStatus
    | UnitStatus
    | AreaStatus
    | ThermostatStatus
    | AuxSensorStatus
    | AudioZoneStatus
    | AudioSourceStatus
    | UserSettingStatus
)
