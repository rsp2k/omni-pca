"""Decryption + parsing for PC Access ``.pca`` and ``PCA01.CFG`` files.

These files are NOT AES-encrypted despite the existence of clsAES — they
use a Borland-Pascal-style 32-bit LCG keystream XORed per byte. Hardcoded
keys are baked into PC Access for the export format and the in-process
config; per-installation ``.pca`` exports use a 32-bit key stored inside
``PCA01.CFG`` (``pca_key`` field).

For the ``.pca`` body we walk header → SetupData → flags → Names → Voices
→ Programs → EventLog → Connection block; the Connection block is what
yields the panel's network address, TCP port, and 16-byte AES-128
ControllerKey used for the live secure-session handshake.

References:
    clsPcaCryptFileStream.cs (lines 88-92) — Borland LCG XOR keystream
    clsPcaCfg.cs — keyPC01 / keyExport constants, PCA01.CFG layout
    clsHAC.cs:7943-8056 — .pca header + body walker, Connection block
    clsCapOMNI_PRO_II.cs — per-model size constants used by the body walker

Cross-references (HAI OmniPro II Installation Manual):
    *INSTALLER SETUP* (pca-re/docs/manuals/installation_manual/
    04_INSTALLER_SETUP/) is what populates everything we then read back
    from a .pca export:
        SETUP CONTROL   → SetupData block (panel-wide options)
        SETUP ZONES     → Names section (zone-name entries) + Z*_TYPE
                          (encoded inside SetupData)
        SETUP AREAS     → Names section (area-name entries) + per-area
                          delays and codes in SetupData
        SETUP MISC      → Programs section (timed scenes, energy savers)
        SETUP EXPANSION → cap counters that drive how big each names
                          block is on the wire
    APPENDIX C — ZONE AND UNIT MAPPING (12_APPENDIX_C_-_ZONE_AND_UNIT_MAPPING/)
    documents the address-space layout the cap constants in
    _CAP_OMNI_PRO_II below derive from (176 zones, 511 units, etc.).
"""

from __future__ import annotations

import io
import os
import struct
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from .programs import (
    MAX_PROGRAMS,
    PROGRAM_BYTES,
    Program,
    decode_program_table,
)

_log = logging.getLogger(__name__)

KEY_PC01: Final[int] = 0x14326573  # 338847091 — clsPcaCfg.keyPC01 (PCA01.CFG)
KEY_EXPORT: Final[int] = 0x17569237  # 391549495 — clsPcaCfg.keyExport (.pca import/export)

_LCG_MULT: Final[int] = 134775813
_MASK32: Final[int] = 0xFFFFFFFF

HEADER_LEN: Final[int] = 2191


def _keystream_byte(seed: int) -> tuple[int, int]:
    seed = (seed * _LCG_MULT + 1) & _MASK32
    # Reference: clsPcaCryptFileStream.cs:88-92 — the `% 255` (not 256) is
    # an intentional Borland Random() quirk; the keystream byte never
    # produces 0xFF.
    return seed, (seed >> 16) % 255


def decrypt_pca_bytes(data: bytes, key: int) -> bytes:
    """XOR-decrypt ``data`` using the Borland LCG keystream seeded with ``key``."""
    seed = key & _MASK32
    out = bytearray(len(data))
    for i, b in enumerate(data):
        seed, ks = _keystream_byte(seed)
        out[i] = b ^ ks
    return bytes(out)


def derive_key_from_stamp(stamp: str) -> int:
    """clsPcaCfg.SetSecurityStamp — fold each character into a 32-bit accumulator.

    Useful only if the original installer stamp string is known.
    """
    k = 0x12345678
    for ch in stamp:
        c = ord(ch) & 0xFF
        k = (((k ^ c) << 7) ^ c) & _MASK32
    return k


class PcaReader:
    """Buffered byte reader matching clsPcaCryptFileStream's read API.

    All multi-byte integers are little-endian; both String8 and String16
    are length-prefixed UTF-8. The fixed-length String variants always
    consume `1 + max_len` (or `2 + max_len`) bytes regardless of declared
    string length — the slack is zero-padded by the writer.
    """

    __slots__ = ("buf",)

    def __init__(self, plaintext: bytes) -> None:
        self.buf = io.BytesIO(plaintext)

    def position(self) -> int:
        return self.buf.tell()

    def remaining(self) -> bytes:
        return self.buf.read()

    def bytes_(self, n: int) -> bytes:
        b = self.buf.read(n)
        if len(b) != n:
            raise EOFError(f"short read: wanted {n}, got {len(b)} at offset {self.buf.tell()}")
        return b

    def u8(self) -> int:
        return self.bytes_(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.bytes_(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.bytes_(4))[0]

    def string8(self) -> str:
        n = self.u8()
        return self.bytes_(n).decode("utf-8", errors="replace")

    def string8_fixed(self, max_len: int) -> str:
        n = self.u8()
        eff = min(n, max_len)
        s = self.bytes_(max_len)[:eff].decode("utf-8", errors="replace")
        return s.rstrip("\x00")

    def string16(self) -> str:
        n = self.u16()
        return self.bytes_(n).decode("utf-8", errors="replace")

    def string16_fixed(self, max_len: int) -> str:
        n = self.u16()
        eff = min(n, max_len)
        s = self.bytes_(max_len)[:eff].decode("utf-8", errors="replace")
        return s.rstrip("\x00")


@dataclass
class PcaConfig:
    """Decoded ``PCA01.CFG`` (PC Access app-level settings)."""

    version_tag: str
    version: int
    init_cmd1: str = ""
    init_cmd2: str = ""
    init_cmd3: str = ""
    local_cmd: str = ""
    online_cmd: str = ""
    answer_cmd: str = ""
    hangup_cmd: str = ""
    modem_port: int = 0
    modem_irq: int = 0
    modem_baud_code: int | None = None
    pca_key: int = 0
    password: str = field(default="", repr=False)
    printer_port: int = 0
    serial_port: int = 0
    serial_baud_code: int = 0


@dataclass
class PcaAccount:
    """Decoded ``.pca`` header + extracted Connection-block fields.

    PII fields (account_name/address/phone/code, plus user code values) are
    intentionally `repr=False` so they don't end up in logs by accident.
    Dialer-side identification of the panel is in the Connection block:
    ``network_address``, ``network_port``, ``controller_key``.
    """

    version_tag: str
    file_version: int
    model: int
    firmware_major: int
    firmware_minor: int
    firmware_revision: int
    network_address: str | None = None
    network_port: int | None = None
    controller_key: bytes | None = None
    account_name: str = field(default="", repr=False)
    account_address: str = field(default="", repr=False)
    account_phone: str = field(default="", repr=False)
    account_code: str = field(default="", repr=False)
    account_remarks: str = field(default="", repr=False)
    programs: tuple[Program, ...] = ()
    """Decoded panel automation programs (1500 slots; many usually empty).

    Populated only when the .pca body is walked successfully and the
    Programs block decodes without error. Empty tuple otherwise. Use
    :func:`omni_pca.programs.iter_defined` to filter to in-use slots.
    """

    remarks: dict[int, str] = field(default_factory=dict)
    """Resolved RemarkID → text for every Remark-typed program.

    A Remark-typed program record (``ProgramType.REMARK``, byte 0 == 4)
    stores a 32-bit BE RemarkID in bytes 1-4 of its 14-byte body; the
    associated user-entered text lives in a separate table further down
    the .pca body (after Connection + ModemBaud flags + nine 33-byte
    Description blocks). The walker parses that table on a best-effort
    basis — failure here doesn't break Connection extraction.
    """

    # Free-text "account remarks" block (a PCA03 extension; appears
    # between the ModemBaud flags and the nine Description blocks).
    # Used by PC Access for installer notes about the site.
    account_remarks_extended: str = field(default="", repr=False)

    # Per-object Description tables — free-text "description" strings
    # entered alongside each object's name in PC Access (e.g. zone 1's
    # name is "FRONT DOOR" and its description might be "Solid wood,
    # contacts at top hinge"). Keys are 1-based slot numbers, values
    # are decoded UTF-8 strings (max 32 chars). Empty slots are
    # omitted. Populated only for FileVersion >= 3.
    zone_descriptions: dict[int, str] = field(default_factory=dict)
    unit_descriptions: dict[int, str] = field(default_factory=dict)
    button_descriptions: dict[int, str] = field(default_factory=dict)
    code_descriptions: dict[int, str] = field(default_factory=dict)
    thermostat_descriptions: dict[int, str] = field(default_factory=dict)
    area_descriptions: dict[int, str] = field(default_factory=dict)
    message_descriptions: dict[int, str] = field(default_factory=dict)
    audio_source_descriptions: dict[int, str] = field(default_factory=dict)
    audio_zone_descriptions: dict[int, str] = field(default_factory=dict)

    # Per-object name tables — populated from the Names block between
    # SetupData and Voices. Keys are 1-based slot numbers; empty slots
    # are omitted entirely (the panel stores them as length-0 String8
    # blobs, which we filter at read time). Other object properties
    # come from the SetupData block (see ``zone_types`` for the first
    # such field extracted).
    zone_names: dict[int, str] = field(default_factory=dict)
    unit_names: dict[int, str] = field(default_factory=dict)
    button_names: dict[int, str] = field(default_factory=dict)
    code_names: dict[int, str] = field(default_factory=dict)
    thermostat_names: dict[int, str] = field(default_factory=dict)
    area_names: dict[int, str] = field(default_factory=dict)
    message_names: dict[int, str] = field(default_factory=dict)

    # Zone types from SetupData installer section. Keys are 1-based slot
    # numbers (always 1..numZones); values are raw ``enuZoneType`` byte
    # values — see ``enuZoneType.cs`` for the full enum. Common values:
    # 0x00=EntryExit, 0x01=Perimeter, 0x03=AwayInt, 0x40=Auxiliary (the
    # panel default for unused slots), 0x55=Extended_Range_OutdoorTemp.
    # Empty dict when SetupData wasn't walked successfully.
    zone_types: dict[int, int] = field(default_factory=dict)

    # Per-zone area assignment from SetupData installer section. Keys
    # are 1-based slot numbers (always 1..numZones); values are 1-based
    # area numbers (1..numAreas). Most single-area installs assign every
    # zone to area 1. Empty dict when SetupData wasn't walked successfully.
    zone_areas: dict[int, int] = field(default_factory=dict)

    # Per-area entry/exit delay (seconds) from SetupData user section.
    # Keys are 1-based area numbers (1..numAreas); typical values are
    # 30/60 (entry) and 60/90 (exit). Unused areas carry the panel
    # default (15 s in the live fixture).
    area_entry_delays: dict[int, int] = field(default_factory=dict)
    area_exit_delays: dict[int, int] = field(default_factory=dict)

    # Per-area boolean configuration flags from SetupData user section,
    # five contiguous bool[8] arrays at offset 1787..1826
    # (clsHAC.cs:3020-3038). Keys are 1-based area numbers.
    #
    #   entry_chime      — chime keypads when entry-delay zones trip
    #   quick_arm        — allow arming without a code
    #   auto_bypass      — silently bypass not-ready zones on arm
    #   all_on_for_alarm — fire every output when any alarm trips
    #   trouble_beep     — beep keypads on a non-alarm trouble condition
    #
    # PerimeterChime and AudibleExitDelay are NOT in this contiguous
    # block — they live deeper in the user section past FlashLightNum,
    # HouseCodes flags, and 6 TimeClock When-structs (see
    # perimeter_chime / audible_exit_delay below).
    area_entry_chime: dict[int, bool] = field(default_factory=dict)
    area_quick_arm: dict[int, bool] = field(default_factory=dict)
    area_auto_bypass: dict[int, bool] = field(default_factory=dict)
    area_all_on_for_alarm: dict[int, bool] = field(default_factory=dict)
    area_trouble_beep: dict[int, bool] = field(default_factory=dict)
    area_perimeter_chime: dict[int, bool] = field(default_factory=dict)
    area_audible_exit_delay: dict[int, bool] = field(default_factory=dict)

    # Per-unit type + area assignment derived from CAP index ranges and
    # the AreaGroups arrays in SetupData. Keys are 1-based unit slots
    # (1..numUnits). Values:
    #   unit_types[u]: raw ``enuOL2UnitType`` byte (1=Standard for X10,
    #     12=Flag for FlagOut, 13=Output for VoltOut/ExpEnc). The
    #     X10 sub-types are collapsed to Standard — deriving the
    #     specific HouseCodeFormat would require the EnableExtCode
    #     table which we don't decode yet.
    #   unit_areas[u]: 8-bit area-membership bitmask. 0xFF is the panel
    #     default ("all areas") when no specific restriction is set;
    #     0x01 means "area 1 only".
    unit_types: dict[int, int] = field(default_factory=dict)
    unit_areas: dict[int, int] = field(default_factory=dict)

    # User codes (PIN database). 99 entries on OMNI_PRO_II.
    #
    # ``code_pins`` is the raw 16-bit value stored on disk (BE u16) —
    # plain 4-digit PINs decode as decimal 0..9999, but the live fixture
    # has some entries with values >9999 whose format isn't yet
    # determined (possibly scrambled, possibly card-credential format,
    # possibly partial-byte flags). Treat as opaque pending RE.
    #
    # ``code_pins`` is marked ``repr=False`` so a debug ``print(acct)``
    # never leaks PIN material into logs. ``code_authority`` is the
    # enuCodeAuthority byte (0=Disabled, 1=User, 2=Manager, 3=Installer)
    # and ``code_areas`` is the area-membership bitmask (0xFF = all).
    code_pins: dict[int, int] = field(default_factory=dict, repr=False)
    code_authority: dict[int, int] = field(default_factory=dict)
    code_areas: dict[int, int] = field(default_factory=dict)

    # HouseCodes.EnableExtCode array (16 bytes on OMNI_PRO_II — one per
    # 16-unit X10 group). Values are raw ``enuHouseCodeFormat`` bytes:
    # 0=Standard, 1=Extended, 2=Compose, 3=UPB, 4=RadioRA, 5=HLC,
    # 6=CentraLite, 7=ZWave, 8=LutronHomeWorks, 9=Clipsal_C_Bus,
    # 10=Dynalite, 11=RadioRA2, 12=Somfy_SDN, 13=ZigBee, 14=KNX,
    # 15=LumaNet, 16=Somfy_URTSI. ``unit_types`` for X10 units uses
    # this table to resolve specific sub-types (HLCRoom vs HLCLoad,
    # ViziaRoomController vs ViziaLoad, etc.) per clsUnit.CalculateUnitType.
    house_code_formats: dict[int, int] = field(default_factory=dict)

    # Three panel-wide time-clock schedules (TimeClock 1/2/3), each as
    # an (On, Off) pair. Tuple of six TimeClocks in order:
    # TC1.On, TC1.Off, TC2.On, TC2.Off, TC3.On, TC3.Off. Empty tuple
    # when SetupData wasn't walked successfully.
    time_clocks: tuple[TimeClock, ...] = ()

    # Two panel-wide PINs for authenticated config access. PII —
    # ``repr=False`` so they never leak into ``print(acct)``. Both are
    # BE u16 ("4-digit decimal"). ``enable_pc_access`` is the toggle
    # that lets a PC Access client connect at all.
    installer_code: int = field(default=0, repr=False)
    enable_pc_access: bool = False
    pc_access_code: int = field(default=0, repr=False)

    # Panel geographic configuration — raw bytes used by the firmware
    # to compute sunrise/sunset for time-of-day programs. No N/S/E/W
    # modifier at this position (those live in the WorldWideLatitude
    # feature block past DST). TimeZone is hours west of UTC on
    # OMNI_PRO_II (7=PDT, 8=PST).
    latitude: int = 0
    longitude: int = 0
    time_zone: int = 0

    # DST configuration (when the panel switches between DST and standard
    # time). Values are raw bytes from enuDSTMonth / enuDSTWeek:
    # 0 = Disabled, 1..12 = month, 1..7 = week (1=First Sunday, 2=Second,
    # 3=Third, 4=Fourth, 5=Last, 6=Next to Last, 7=Third from Last).
    # US default after 2007: Mar/Second, Nov/First.
    dst_start_month: int = 0
    dst_start_week: int = 0
    dst_end_month: int = 0
    dst_end_week: int = 0

    # Panel-wide TempFormat (enuTempFormat: 1=Fahrenheit, 2=Celsius)
    # and NumAreasUsed (count of armable security areas — 1 for a
    # typical single-area home install, up to numAreas=8 on Omni Pro II).
    # Both are 0 if SetupData wasn't walked successfully.
    temp_format: int = 0
    num_areas_used: int = 0


def parse_pca01_cfg(data: bytes, key: int = KEY_PC01) -> PcaConfig:
    """Decrypt ``data`` (raw PCA01.CFG bytes) and parse per clsPcaCfg.Read()."""
    plain = decrypt_pca_bytes(data, key)
    r = PcaReader(plain)
    version_tag = r.string8()
    try:
        version = int(version_tag[3:] or "0")
    except ValueError as exc:
        raise ValueError(f"unrecognized CFG version tag {version_tag!r}") from exc
    if version < 4:
        raise ValueError(f"unsupported CFG version {version} (need >= 4)")

    cfg = PcaConfig(version_tag=version_tag, version=version)
    cfg.init_cmd1 = r.string8_fixed(40)
    if version >= 5:
        cfg.init_cmd2 = r.string8_fixed(40)
        cfg.init_cmd3 = r.string8_fixed(40)
    cfg.local_cmd = r.string8_fixed(40)
    cfg.online_cmd = r.string8_fixed(40)
    cfg.answer_cmd = r.string8_fixed(40)
    cfg.hangup_cmd = r.string8_fixed(40)
    cfg.modem_port = r.u16()
    cfg.modem_irq = r.u16()
    if version < 5:
        cfg.modem_baud_code = r.u16()
    cfg.pca_key = r.u32()
    cfg.password = r.string8_fixed(10)
    cfg.printer_port = r.u16()
    cfg.serial_port = r.u16()
    cfg.serial_baud_code = r.u16()
    return cfg


# OMNI_PRO_II capability constants (clsCapOMNI_PRO_II.cs). For other models
# this table needs broadening — but Connection-block extraction only requires
# correct totals up to that block, and OMNI_PRO_II is the working reference.
_CAP_OMNI_PRO_II: dict[str, int] = {
    "lenSetupData": 3840,
    # User section walks from offset 1 (Seek(1)). Fixed-width derivation:
    #
    #   1..5: TelephoneAccess+AnswerOutsideCall+RemoteCommandsOK+
    #         RingsBeforeAnswer+DialMode (5×1 byte)
    #   6..30: MyPhoneNumber (25 = 1 length + 24 payload, fixed-width)
    #   31..310: Phone[0..7] (8 × 35: Number(25)+WhenOn(5)+WhenOff(5))
    #   311..382: Areas[1..8].DialOrder (8 × 9 fixed-width String8)
    #   383..1768: Codes[1..99] (99 × 14: Code(u16)+Authority(1)+
    #              Areas(1)+WhenOn(5)+WhenOff(5))
    #   1769..1770: Codes[251].Code (u16)
    #   1771..1778: Areas[1..8].EntryDelay (8 bytes)
    #   1779..1786: Areas[1..8].ExitDelay (8 bytes)
    #
    # Codes block: 99 entries × 14 bytes at offset 383.
    # Per-entry layout (clsHAC.cs:3001-3009):
    #   bytes 0..1: PIN (BE u16, clsHardwareArray.ReadUInt16)
    #   byte 2:     Authority (enuCodeAuthority: 0=Disabled, 1=User,
    #               2=Manager, 3=Installer)
    #   byte 3:     Areas bitmask
    #   bytes 4..8: WhenOn (clsWhen)
    #   bytes 9..13: WhenOff (clsWhen)
    "codesOffset": 383,
    "codeEntryBytes": 14,
    "entryDelayOffset": 1771,
    "exitDelayOffset": 1779,
    # Five contiguous bool[8] flag arrays immediately follow ExitDelay
    # (clsHAC.cs:3020-3038). PerimeterChime and AudibleExitDelay are
    # NOT contiguous — they live later, past HighSecurity, FreezeAlarm,
    # FlashLightNum, HouseCodes flags, and 6 TimeClock When-structs.
    "entryChimeOffset": 1787,
    "quickArmOffset": 1795,
    "autoBypassOffset": 1803,
    "allOnForAlarmOffset": 1811,
    "troubleBeepOffset": 1819,
    # After TroubleBeep the user section continues with HighSecurity (1),
    # FreezeAlarm (1), FlashLightNum_HI+LO (2; lastX10>255), HouseCodes
    # EnableAllOff[16] (16), EnableAllOn[16] (16), 6×clsWhen (30),
    # Latitude+Longitude+TimeZone (3), AnnounceAlarms (1) — 70 bytes —
    # then the two remaining area bool[8] flags and DST scalars:
    #   1897..1904: PerimeterChime[1..8]
    #   1905..1912: AudibleExitDelay[1..8]
    #   1913: DSTStartMonth (enuDSTMonth)
    #   1914: DSTStartWeek  (enuDSTWeek)
    #   1915: DSTEndMonth   (enuDSTMonth)
    #   1916: DSTEndWeek    (enuDSTWeek)
    # HouseCodes.Count derives as (lastX10 - firstX10 + 1) / 16 = 16 for
    # OMNI_PRO_II (clsHouseCodes.cs:35).
    # Three single-byte scalars sandwiched between the TimeClocks block
    # and AnnounceAlarms (clsHAC.cs:3064-3066). Latitude / Longitude are
    # raw degrees (no N/S/E/W modifier in this position — those live in
    # the WorldWideLatitude feature block after DST). TimeZone is the
    # panel's UTC offset selector; OMNI_PRO_II uses raw hours west of
    # UTC (e.g. 7 = Pacific Daylight, 8 = Pacific Standard).
    "latitudeOffset": 1893,
    "longitudeOffset": 1894,
    "timeZoneOffset": 1895,
    "perimeterChimeOffset": 1897,
    "audibleExitDelayOffset": 1905,
    "dstStartMonthOffset": 1913,
    "dstStartWeekOffset": 1914,
    "dstEndMonthOffset": 1915,
    "dstEndWeekOffset": 1916,
    # HouseCodes.EnableExtCode[1..16] (1 byte/HouseCode, raw
    # enuHouseCodeFormat). Read order is right after the 4 DST bytes
    # per clsHAC.cs:3084-3088. Live fixture: [5,1,1,...,1] = HouseCode 1
    # is HLC, the rest are Extended.
    "houseCodeFormatOffset": 1917,
    # Six 5-byte clsWhen structs in order TC1.On, TC1.Off, TC2.On,
    # TC2.Off, TC3.On, TC3.Off (clsHAC.cs:3058-3063, before
    # Latitude/Longitude/TimeZone).
    "timeClocksOffset": 1863,
    # InstallerCode/PCAccessCode are BE u16 inside the installer
    # section, sandwiching the EnablePCAccess bool at offset 2997.
    "installerCodeOffset": 2995,
    "enablePCAccessOffset": 2997,
    "pcAccessCodeOffset": 2998,

    # Installer section begins at byte 2560 (clsCapOMNI_PRO_II.instSetupStart).
    # Layout for OMNI_PRO_II observed empirically against the live fixture
    # and cross-checked against clsHAC._ParseSetupData (clsHAC.cs:3156-...).
    #
    #   2560: HouseCode (1 byte)
    #   2561..2569: OutputType[0..8] (9 bytes; numVoltOutputs)
    #   2570: ZoneExpansions (1 byte)
    #   2571: NumExpEnc (1 byte; firstExpEncOut != 0)
    #   2572..2747: ZoneType[1..176] (176 bytes; raw enuZoneType byte/zone)
    #   2748..2772: DCMPhoneNumber1 (25-byte fixed-width string)
    #   2773..2774: DCMAccount1 (u16)
    #   2775..2799: DCMPhoneNumber2 (25)
    #   2800..2801: DCMAccount2 (u16)
    #   2802: DCMType (1)
    #   2803..2807: DCMTestTime (5-byte clsWhen: Hr,Min,Mon,Day,DOW)
    #   2808: DCMTestCode (1)
    #   2809..2984: Zones[].DCMAlarmCode (176 bytes)
    #   2985..2992: DCMFreezeAlm/Fire/Police/Aux/Duress/BatteryLow/FireZone/Cancel (8)
    #   2993..3004: TempFormat, NumThermostats, InstallerCode(u16),
    #               EnablePCAccess, PCAccessCode(u16), ...
    #   2993: TempFormat / 2994: NumThermostats / 2995..2996: InstallerCode
    #   2997: EnablePCAccess / 2998..2999: PCAccessCode
    #   3000..3024: CallBackNumber (25)
    #   3025: ExteriorHornDelay / 3026: DialoutDelay / 3027: VerifyFireAlarms
    #   3028: EnableConsoleEmg / 3029: TimeFormat / 3030: DateFormat
    #   3031: ACPowerFreq / 3032: DeadLineDetect / 3033: OffHookDetect
    #   3034: NumAreasUsed
    #   3035..3050: X10 AreaGroups (16 bytes; (lastX10-firstX10+16)/16)
    #   3051..3058: VoltOut AreaGroups (8; lastVoltOut-firstVoltOut+1)
    #   3059..3073: FlagOut AreaGroups (15; (lastFlagOut-firstFlagOut+8)/8)
    #   3074..3105: ExpEnc AreaGroups (32; (lastExpEncOut-firstExpEncOut+4)/4)
    #   3106..3281: Zones[1..176].Area (176 bytes — area number per zone)
    #
    # Hardcoded for OMNI_PRO_II — other panels will need their own values.
    "instSetupStart": 2560,
    "zoneTypeOffset": 2572,
    "tempFormatOffset": 2993,
    "numAreasUsedOffset": 3034,
    # AreaGroups arrays per family — each byte is an 8-bit area-membership
    # bitmask covering one or more units, sized via the CAP ranges:
    "x10AreaGroupsOffset": 3035,      # (lastX10-firstX10+16)/16 = 16 bytes, 1 group/16 units
    "voltOutAreaGroupsOffset": 3051,  # lastVoltOut-firstVoltOut+1 = 8 bytes, 1 byte/unit
    "flagOutAreaGroupsOffset": 3059,  # (lastFlagOut-firstFlagOut+8)/8 = 15 bytes, 1 group/8 flags
    "expEncAreaGroupsOffset": 3074,   # (lastExpEncOut-firstExpEncOut+4)/4 = 32 bytes, 1 group/4 outputs
    "zoneAreaOffset": 3106,
    # Unit index ranges → unit type derivation. Per CAP for OMNI_PRO_II:
    "firstX10": 1, "lastX10": 256,
    "firstExpEncOut": 257, "lastExpEncOut": 384,
    "firstVoltOut": 385, "lastVoltOut": 392,
    "firstFlagOut": 393, "lastFlagOut": 511,
    "max_zones": 176, "lenZoneName": 15, "zones_count": 176,
    "max_units": 512, "lenUnitName": 12, "units_count": 511,
    "max_buttons": 128, "lenButtonName": 12, "buttons_count": 255,
    "max_codes": 99, "lenCodeName": 12, "codes_count": 99,
    "max_tstats": 64, "lenTstatName": 12, "tstats_count": 64,
    "max_areas": 8, "lenAreaName": 12, "areas_count": 8,
    "max_messages": 128, "lenMessageName": 15, "messages_count": 128,
    "max_message_voices": 128,
    "voice_struct_bytes": 12,
    "voice_skip_bytes": 6,
    "max_programs": 1500, "program_bytes": 14,
    "max_event_log": 250, "event_bytes": 9,
}


def _parse_header(r: PcaReader) -> tuple[str, int, int, int, int, int, str, str, str, str, str]:
    """Read the fixed 2191-byte PCA03 header, returning (version_tag, file_version,
    model, fw_major, fw_minor, fw_rev, name, address, phone, code, remarks)."""
    version_tag = r.string8()
    try:
        file_version = int(version_tag[3:] or "0")
    except ValueError as exc:
        raise ValueError(f"unrecognized .pca version tag {version_tag!r}") from exc
    name = r.string8_fixed(30)
    address = r.string16_fixed(120)
    phone = r.string8_fixed(20)
    code = r.string8_fixed(4)
    remarks = r.string16_fixed(2000)
    if r.position() != HEADER_LEN - 4:
        raise ValueError(f"header parse misaligned at offset {r.position()}, expected 2187")
    model = r.u8()
    fw_major = r.u8()
    fw_minor = r.u8()
    fw_rev = struct.unpack("<b", r.bytes_(1))[0]
    return version_tag, file_version, model, fw_major, fw_minor, fw_rev, name, address, phone, code, remarks


# Description blocks each store a 32-byte fixed slot prefixed by a
# 1-byte length (clsAbstractNamedItem.ReadDescription:1-4) so each
# entry is exactly _DESCRIPTION_SLOT_BYTES on the wire regardless of
# the actual string length.
_DESCRIPTION_SLOT_BYTES: Final[int] = 1 + 32


@dataclass
class _RemarksWalk:
    """Side-channel output of :func:`_walk_to_remarks`.

    Captures everything in the post-Connection PCA03 extension:
    AccountRemarks_Extended (free text), the nine per-family
    Description tables, and the Remarks ID→text dict that
    Remark-typed programs reference.
    """

    account_remarks_extended: str = ""
    zone_descriptions: dict[int, str] = field(default_factory=dict)
    unit_descriptions: dict[int, str] = field(default_factory=dict)
    button_descriptions: dict[int, str] = field(default_factory=dict)
    code_descriptions: dict[int, str] = field(default_factory=dict)
    thermostat_descriptions: dict[int, str] = field(default_factory=dict)
    area_descriptions: dict[int, str] = field(default_factory=dict)
    message_descriptions: dict[int, str] = field(default_factory=dict)
    audio_source_descriptions: dict[int, str] = field(default_factory=dict)
    audio_zone_descriptions: dict[int, str] = field(default_factory=dict)
    remarks: dict[int, str] = field(default_factory=dict)


def _read_description_table(r: PcaReader) -> dict[int, str]:
    """Read a per-family Description block: u32 count, then count ×
    String8(32). Returns {1-based slot: description} omitting empties.

    Mirrors ``clsZones.ReadDescription`` and friends — the format is
    identical across object families.
    """
    count = r.u32()
    out: dict[int, str] = {}
    for i in range(1, count + 1):
        text = r.string8_fixed(32)
        if text:
            out[i] = text
    return out


def _walk_to_remarks(r: PcaReader) -> _RemarksWalk:
    """Walk the PCA03 post-Connection extension.

    Layout for PCA03 (FileVersion 3, per clsHAC.cs:8058-8079):

    1. ``ModemBaud`` (u16 LE), 3× bool (1 byte each), AccountRemarks_Extended
       (String16: u16 length + bytes).
    2. Nine Description blocks in the order Zones, Units, Buttons, Codes,
       Thermostats, Areas, Messages, AudioSources, AudioZones. Each is
       ``[u32 count] + count * 33 bytes`` (per :data:`_DESCRIPTION_SLOT_BYTES`).
       The 33-byte slots are String8(32) — 1 length byte + 32 padded bytes.
    3. Remarks table:
           - ``_RemarksNextID`` (u32 LE) — what ``RemarksNextID()`` will
             hand out next.
           - ``count`` (u32 LE).
           - ``count`` entries of ``[u32 LE remark_id][String16 text]``.

    Returns an empty :class:`_RemarksWalk` on any read failure (panels
    without these blocks, or a file format we don't recognise).

    Reference: clsPrograms.ReadRemarks (clsPrograms.cs:148-168),
    clsAbstractNamedItem.ReadDescription, clsHAC.cs:8058-8079.
    """
    walk = _RemarksWalk()
    try:
        r.u16()                  # ModemBaud
        r.u8(); r.u8(); r.u8()   # PCModemInit1/2/3 enable flags
        walk.account_remarks_extended = r.string16()
        walk.zone_descriptions = _read_description_table(r)
        walk.unit_descriptions = _read_description_table(r)
        walk.button_descriptions = _read_description_table(r)
        walk.code_descriptions = _read_description_table(r)
        walk.thermostat_descriptions = _read_description_table(r)
        walk.area_descriptions = _read_description_table(r)
        walk.message_descriptions = _read_description_table(r)
        walk.audio_source_descriptions = _read_description_table(r)
        walk.audio_zone_descriptions = _read_description_table(r)
        # Remarks table.
        r.u32()                  # _RemarksNextID
        remark_count = r.u32()
        for _ in range(remark_count):
            rid = r.u32()
            text = r.string16()
            walk.remarks[rid] = text
        return walk
    except (EOFError, ValueError, struct.error):
        return walk


@dataclass(frozen=True, slots=True)
class TimeClock:
    """A panel time-clock schedule (``clsWhen``).

    Five raw bytes from SetupData. ``hour``/``minute`` are 0..23/0..59.
    ``month``/``day`` are 0 when the entry repeats by day-of-week
    rather than a fixed date. ``days`` is the raw ``enuDays`` bitmask
    where bit 1=Mon, bit 2=Tue, bit 3=Wed, bit 4=Thu, bit 5=Fri,
    bit 6=Sat, bit 7=Sun (bit 0 unused). 0xFE = every day; 0x00 = the
    entry is unscheduled / disabled.
    """

    hour: int
    minute: int
    month: int
    day: int
    days: int

    @classmethod
    def parse(cls, data: bytes) -> TimeClock:
        if len(data) < 5:
            return cls(0, 0, 0, 0, 0)
        return cls(data[0], data[1], data[2], data[3], data[4])


@dataclass
class _ConnectionWalk:
    """Side-channel output of :func:`_walk_to_connection`.

    Captures the per-object name tables + selected SetupData fields on
    the way past so the caller can attach them to :class:`PcaAccount`.
    Each ``*_names`` dict is ``{1-based slot: name}`` with only non-empty
    slots present — matches the "iter_defined" convention used for
    programs. ``zone_types`` is ``{1-based slot: enuZoneType byte}`` for
    every zone slot (defined or not — the array is fixed-size).
    """

    programs_blob: bytes
    zone_names: dict[int, str] = field(default_factory=dict)
    unit_names: dict[int, str] = field(default_factory=dict)
    button_names: dict[int, str] = field(default_factory=dict)
    code_names: dict[int, str] = field(default_factory=dict)
    thermostat_names: dict[int, str] = field(default_factory=dict)
    area_names: dict[int, str] = field(default_factory=dict)
    message_names: dict[int, str] = field(default_factory=dict)
    zone_types: dict[int, int] = field(default_factory=dict)
    zone_areas: dict[int, int] = field(default_factory=dict)
    area_entry_delays: dict[int, int] = field(default_factory=dict)
    area_exit_delays: dict[int, int] = field(default_factory=dict)
    area_entry_chime: dict[int, bool] = field(default_factory=dict)
    area_quick_arm: dict[int, bool] = field(default_factory=dict)
    area_auto_bypass: dict[int, bool] = field(default_factory=dict)
    area_all_on_for_alarm: dict[int, bool] = field(default_factory=dict)
    area_trouble_beep: dict[int, bool] = field(default_factory=dict)
    area_perimeter_chime: dict[int, bool] = field(default_factory=dict)
    area_audible_exit_delay: dict[int, bool] = field(default_factory=dict)
    unit_types: dict[int, int] = field(default_factory=dict)
    unit_areas: dict[int, int] = field(default_factory=dict)
    code_pins: dict[int, int] = field(default_factory=dict, repr=False)
    code_authority: dict[int, int] = field(default_factory=dict)
    code_areas: dict[int, int] = field(default_factory=dict)
    house_code_formats: dict[int, int] = field(default_factory=dict)
    time_clocks: tuple[TimeClock, ...] = ()
    installer_code: int = 0
    enable_pc_access: bool = False
    pc_access_code: int = 0
    latitude: int = 0
    longitude: int = 0
    time_zone: int = 0
    dst_start_month: int = 0
    dst_start_week: int = 0
    dst_end_month: int = 0
    dst_end_week: int = 0
    temp_format: int = 0
    num_areas_used: int = 0


def _read_name_table(r: PcaReader, count: int, name_len: int) -> dict[int, str]:
    """Read ``count`` String8(name_len) slots; return only non-empty ones.

    Per-slot layout per ``clsAbstractNamedItem.ReadName`` /
    ``clsPcaCryptFileStream.ReadString8(out S, byte L)``:

      ``[1 byte actual length][name_len bytes name]``

    The length byte is 0 for unused slots. We use ``string8_fixed`` to
    consume exactly ``1 + name_len`` bytes per slot regardless.
    """
    out: dict[int, str] = {}
    for i in range(1, count + 1):
        name = r.string8_fixed(name_len)
        if name:
            out[i] = name
    return out


def _walk_to_connection(r: PcaReader, cap: dict[str, int]) -> _ConnectionWalk:
    """Walk SetupData, flags, Names, Voices, Programs, EventLog so the
    next read lands on the Connection block. Captures per-object name
    tables on the way past and returns them alongside the Programs blob.

    Mirrors clsHAC.cs:7995-8044. The per-object names are read via
    clsAbstractNamedItem.ReadName → String8(L) — see
    :func:`_read_name_table` for the per-slot layout.

    SetupData is captured to a buffer up-front so we can index into its
    installer section for ZoneType (offset 2572 on OMNI_PRO_II).
    """
    setup_data = r.bytes_(cap["lenSetupData"])
    r.bytes_(10)  # bool + bool + u16 + u16 + u32

    # Pull ZoneType and Zones[].Area from the installer section of SetupData.
    # See the comment block on _CAP_OMNI_PRO_II for layout details.
    zt_off = cap.get("zoneTypeOffset")
    zone_types: dict[int, int] = {}
    if zt_off is not None:
        zt_end = zt_off + cap["max_zones"]
        if zt_end <= len(setup_data):
            for slot in range(1, cap["max_zones"] + 1):
                zone_types[slot] = setup_data[zt_off + slot - 1]

    za_off = cap.get("zoneAreaOffset")
    zone_areas: dict[int, int] = {}
    if za_off is not None:
        za_end = za_off + cap["max_zones"]
        if za_end <= len(setup_data):
            for slot in range(1, cap["max_zones"] + 1):
                zone_areas[slot] = setup_data[za_off + slot - 1]

    # Per-area entry/exit delays from the user section.
    num_areas = cap.get("max_areas", 0)
    def _read_area_byte_array(offset_key: str) -> dict[int, int]:
        off = cap.get(offset_key)
        if off is None or off + num_areas > len(setup_data):
            return {}
        return {i: setup_data[off + i - 1] for i in range(1, num_areas + 1)}

    def _read_area_bool_array(offset_key: str) -> dict[int, bool]:
        return {i: bool(b) for i, b in _read_area_byte_array(offset_key).items()}

    area_entry_delays = _read_area_byte_array("entryDelayOffset")
    area_exit_delays = _read_area_byte_array("exitDelayOffset")
    area_entry_chime = _read_area_bool_array("entryChimeOffset")
    area_quick_arm = _read_area_bool_array("quickArmOffset")
    area_auto_bypass = _read_area_bool_array("autoBypassOffset")
    area_all_on_for_alarm = _read_area_bool_array("allOnForAlarmOffset")
    area_trouble_beep = _read_area_bool_array("troubleBeepOffset")
    area_perimeter_chime = _read_area_bool_array("perimeterChimeOffset")
    area_audible_exit_delay = _read_area_bool_array("audibleExitDelayOffset")

    def _read_scalar_byte(offset_key: str) -> int:
        off = cap.get(offset_key)
        if off is None or off >= len(setup_data):
            return 0
        return setup_data[off]

    dst_start_month = _read_scalar_byte("dstStartMonthOffset")
    dst_start_week = _read_scalar_byte("dstStartWeekOffset")
    dst_end_month = _read_scalar_byte("dstEndMonthOffset")
    dst_end_week = _read_scalar_byte("dstEndWeekOffset")

    # HouseCodes.EnableExtCode[1..N] — one byte per X10 house code group.
    # Count derives from CAP as (lastX10 - firstX10 + 1) / 16 = 16 on
    # OMNI_PRO_II (clsHouseCodes.cs:35).
    hcf_off = cap.get("houseCodeFormatOffset")
    f_x10_for_hc = cap.get("firstX10", 0)
    l_x10_for_hc = cap.get("lastX10", 0)
    n_hcf = (l_x10_for_hc - f_x10_for_hc + 1) // 16 if l_x10_for_hc else 0
    house_code_formats: dict[int, int] = {}
    if hcf_off is not None and hcf_off + n_hcf <= len(setup_data):
        for k in range(1, n_hcf + 1):
            house_code_formats[k] = setup_data[hcf_off + k - 1]

    # Six 5-byte clsWhen structs for TimeClock 1/2/3 On/Off.
    tc_off = cap.get("timeClocksOffset")
    time_clocks: tuple[TimeClock, ...] = ()
    if tc_off is not None and tc_off + 30 <= len(setup_data):
        time_clocks = tuple(
            TimeClock.parse(setup_data[tc_off + i * 5 : tc_off + (i + 1) * 5])
            for i in range(6)
        )

    # InstallerCode / PCAccessCode (BE u16) flanking the EnablePCAccess
    # toggle. All three live in the installer section.
    def _read_be_u16(offset_key: str) -> int:
        off = cap.get(offset_key)
        if off is None or off + 2 > len(setup_data):
            return 0
        return (setup_data[off] << 8) | setup_data[off + 1]

    installer_code = _read_be_u16("installerCodeOffset")
    pc_access_code = _read_be_u16("pcAccessCodeOffset")
    latitude = _read_scalar_byte("latitudeOffset")
    longitude = _read_scalar_byte("longitudeOffset")
    time_zone = _read_scalar_byte("timeZoneOffset")
    epa_off = cap.get("enablePCAccessOffset")
    enable_pc_access = (
        bool(setup_data[epa_off])
        if epa_off is not None and epa_off < len(setup_data)
        else False
    )

    # Unit type + area assignment, per unit index.
    #
    # Unit *type* is derived from which CAP range the index falls in
    # (clsUnit.CalculateUnitType + the AreaGroups read in
    # clsHAC._ParseSetupData at clsHAC.cs:3242-3289). We collapse the
    # X10 sub-types (Standard/Extended/HLC/UPB/ZWave/…) to
    # enuOL2UnitType.Standard=1 since deriving them requires the
    # HouseCodes EnableExtCode table; non-X10 families resolve to
    # Output=13 (Voltage/ExpEnc) or Flag=12.
    #
    # Unit *area* is the bitmask byte from the AreaGroups array of the
    # appropriate family, indexed by the unit's group:
    #   X10:     group = (Number - firstX10) // 16
    #   VoltOut: group = (Number - firstVoltOut)                  (1 byte/unit)
    #   FlagOut: group = (Number - firstFlagOut) // 8
    #   ExpEnc:  group = (Number - firstExpEncOut) // 4
    # Byte 0xFF (panel default, uninitialised) is reported verbatim —
    # consumers treat that as "all areas".
    def _read_area_group(group_off_key: str, group_idx: int) -> int:
        off = cap.get(group_off_key)
        if off is None:
            return 0xFF
        pos = off + group_idx
        if pos >= len(setup_data):
            return 0xFF
        return setup_data[pos]

    # Codes block — extract PINs (raw BE u16), authority byte, areas
    # bitmask. PINs are PII; we expose the raw value but don't print it
    # in any repr (PcaAccount uses repr=False on these fields).
    codes_off = cap.get("codesOffset")
    code_bytes = cap.get("codeEntryBytes", 14)
    num_codes = cap.get("max_codes", 0)
    code_pins: dict[int, int] = {}
    code_authority: dict[int, int] = {}
    code_areas: dict[int, int] = {}
    if codes_off is not None:
        for k in range(1, num_codes + 1):
            base = codes_off + (k - 1) * code_bytes
            if base + 4 > len(setup_data):
                break
            # BE u16 (clsHardwareArray.ReadUInt16)
            code_pins[k] = (setup_data[base] << 8) | setup_data[base + 1]
            code_authority[k] = setup_data[base + 2]
            code_areas[k] = setup_data[base + 3]

    # Direct enuHouseCodeFormat → enuOL2UnitType mapping for the
    # non-conditional formats (clsUnit.CalculateUnitType,
    # clsUnit.cs:928-999). HLC and ZWave have a Number-position-based
    # split (Room vs Load); see the inline branches below.
    _HCFMT_TO_UTYPE: dict[int, int] = {
        0: 1,    # Standard       → Standard
        1: 2,    # Extended       → Extended
        2: 3,    # Compose        → Compose
        3: 4,    # UPB            → UPB
        4: 8,    # RadioRA        → RadioRA
        6: 9,    # CentraLite     → Centralite
        8: 16,   # LutronHomeWorks
        9: 17,   # Clipsal_C_Bus
        10: 18,  # Dynalite
        11: 19,  # RadioRA2
        12: 20,  # Somfy_SDN
        13: 21,  # ZigBee
        14: 22,  # KNX
        15: 23,  # LumaNet
        16: 24,  # Somfy_URTSI
    }

    unit_types: dict[int, int] = {}
    unit_areas: dict[int, int] = {}
    f_x10, l_x10 = cap.get("firstX10", 0), cap.get("lastX10", 0)
    f_vo, l_vo = cap.get("firstVoltOut", 0), cap.get("lastVoltOut", 0)
    f_fo, l_fo = cap.get("firstFlagOut", 0), cap.get("lastFlagOut", 0)
    f_ee, l_ee = cap.get("firstExpEncOut", 0), cap.get("lastExpEncOut", 0)
    max_units = cap.get("max_units", 0)
    for u in range(1, max_units + 1):
        if f_x10 and f_x10 <= u <= l_x10:
            # Resolve specific X10 sub-type via the HouseCode containing
            # this unit. House code N covers units (N-1)*16+1..N*16, so
            # ((u - firstX10) // 16) + 1 is the 1-based HouseCode index.
            hc_idx = (u - f_x10) // 16 + 1
            hcfmt = house_code_formats.get(hc_idx, 0)
            if hcfmt == 5:  # HLC
                # HLCRoom (5) if Number-1 is a multiple of 8, else HLCLoad (6).
                unit_types[u] = 5 if (u - 1) % 8 == 0 else 6
            elif hcfmt == 7:  # ZWave
                # ViziaRoomController (10) for "room" position, else ViziaLoad (11).
                # Real-panel ViziaRoomController also requires ZWaveNodeID
                # context the .pca doesn't carry; we approximate with the
                # Number-position rule alone.
                unit_types[u] = 10 if (u - 1) % 8 == 0 else 11
            else:
                unit_types[u] = _HCFMT_TO_UTYPE.get(hcfmt, 1)
            unit_areas[u] = _read_area_group(
                "x10AreaGroupsOffset", (u - f_x10) // 16
            )
        elif f_ee and f_ee <= u <= l_ee:
            unit_types[u] = 13  # enuOL2UnitType.Output
            unit_areas[u] = _read_area_group(
                "expEncAreaGroupsOffset", (u - f_ee) // 4
            )
        elif f_vo and f_vo <= u <= l_vo:
            unit_types[u] = 13  # enuOL2UnitType.Output
            unit_areas[u] = _read_area_group(
                "voltOutAreaGroupsOffset", u - f_vo
            )
        elif f_fo and f_fo <= u <= l_fo:
            unit_types[u] = 12  # enuOL2UnitType.Flag
            unit_areas[u] = _read_area_group(
                "flagOutAreaGroupsOffset", (u - f_fo) // 8
            )

    # Scalars from the installer section.
    tf_off = cap.get("tempFormatOffset")
    temp_format = setup_data[tf_off] if tf_off is not None and tf_off < len(setup_data) else 0
    na_off = cap.get("numAreasUsedOffset")
    num_areas_used = setup_data[na_off] if na_off is not None and na_off < len(setup_data) else 0

    # Object family order per clsHAC body layout:
    # Zones → Units → Buttons → Codes → Thermostats → Areas → Messages.
    zone_names = _read_name_table(r, cap["max_zones"], cap["lenZoneName"])
    unit_names = _read_name_table(r, cap["max_units"], cap["lenUnitName"])
    button_names = _read_name_table(r, cap["max_buttons"], cap["lenButtonName"])
    code_names = _read_name_table(r, cap["max_codes"], cap["lenCodeName"])
    thermostat_names = _read_name_table(r, cap["max_tstats"], cap["lenTstatName"])
    area_names = _read_name_table(r, cap["max_areas"], cap["lenAreaName"])
    message_names = _read_name_table(r, cap["max_messages"], cap["lenMessageName"])

    # Voices: structured slots are 12 B (LargeVocabulary), skip slots 6 B.
    voice_specs = [
        (cap["max_zones"], cap["zones_count"]),
        (cap["max_units"], cap["units_count"]),
        (cap["max_buttons"], cap["buttons_count"]),
        (cap["max_codes"], cap["codes_count"]),
        (cap["max_tstats"], cap["tstats_count"]),
        (cap["max_areas"], cap["areas_count"]),
        (cap["max_message_voices"], cap["messages_count"]),
    ]
    s_b = cap["voice_struct_bytes"]
    k_b = cap["voice_skip_bytes"]
    for max_slots, items_count in voice_specs:
        struct_slots = min(items_count, max_slots)
        skip_slots = max(0, max_slots - items_count)
        r.bytes_(struct_slots * s_b + skip_slots * k_b)

    programs_blob = r.bytes_(cap["max_programs"] * cap["program_bytes"])
    r.bytes_(cap["max_event_log"] * cap["event_bytes"])
    return _ConnectionWalk(
        programs_blob=programs_blob,
        zone_names=zone_names,
        unit_names=unit_names,
        button_names=button_names,
        code_names=code_names,
        thermostat_names=thermostat_names,
        area_names=area_names,
        message_names=message_names,
        zone_types=zone_types,
        zone_areas=zone_areas,
        area_entry_delays=area_entry_delays,
        area_exit_delays=area_exit_delays,
        area_entry_chime=area_entry_chime,
        area_quick_arm=area_quick_arm,
        area_auto_bypass=area_auto_bypass,
        area_all_on_for_alarm=area_all_on_for_alarm,
        area_trouble_beep=area_trouble_beep,
        area_perimeter_chime=area_perimeter_chime,
        area_audible_exit_delay=area_audible_exit_delay,
        unit_types=unit_types,
        unit_areas=unit_areas,
        code_pins=code_pins,
        code_authority=code_authority,
        code_areas=code_areas,
        house_code_formats=house_code_formats,
        time_clocks=time_clocks,
        installer_code=installer_code,
        enable_pc_access=enable_pc_access,
        pc_access_code=pc_access_code,
        latitude=latitude,
        longitude=longitude,
        time_zone=time_zone,
        dst_start_month=dst_start_month,
        dst_start_week=dst_start_week,
        dst_end_month=dst_end_month,
        dst_end_week=dst_end_week,
        temp_format=temp_format,
        num_areas_used=num_areas_used,
    )


def parse_pca_file(path_or_bytes: str | os.PathLike[str] | bytes, key: int) -> PcaAccount:
    """Decrypt and parse a ``.pca`` file. Returns the header + Connection block.

    ``key`` is the 32-bit XOR key — typically ``KEY_EXPORT`` for export files
    or the ``pca_key`` field from a paired ``PCA01.CFG``.
    """
    raw = (
        path_or_bytes
        if isinstance(path_or_bytes, bytes)
        else Path(path_or_bytes).read_bytes()
    )
    plain = decrypt_pca_bytes(raw, key)
    if len(plain) < HEADER_LEN:
        raise ValueError(f"plaintext too small: {len(plain)} < header {HEADER_LEN}")

    r = PcaReader(plain)
    (
        version_tag,
        file_version,
        model,
        fw_major,
        fw_minor,
        fw_rev,
        name,
        address,
        phone,
        code,
        remarks,
    ) = _parse_header(r)

    account = PcaAccount(
        version_tag=version_tag,
        file_version=file_version,
        model=model,
        firmware_major=fw_major,
        firmware_minor=fw_minor,
        firmware_revision=fw_rev,
        account_name=name,
        account_address=address,
        account_phone=phone,
        account_code=code,
        account_remarks=remarks,
    )

    if file_version < 2:
        return account

    try:
        walk = _walk_to_connection(r, _CAP_OMNI_PRO_II)
        network_address = r.string8_fixed(120)
        port_str = r.string8_fixed(5)
        try:
            network_port = int(port_str)
        except ValueError:
            network_port = 4369
        key_hex = r.string8_fixed(32).ljust(32, "0")[:32]
        controller_key = bytes.fromhex(key_hex)
        # Decode the program table — non-fatal if it fails; Connection
        # block has already been read so the network/key fields land
        # regardless. A malformed Programs block likely means the body
        # walker misaligned for a non-OMNI_PRO_II model, in which case
        # leaving programs=() is the honest answer.
        try:
            programs: tuple[Program, ...] = decode_program_table(walk.programs_blob)
        except Exception:
            _log.warning("failed to decode Programs block", exc_info=True)
            programs = ()
    except (EOFError, ValueError):
        # Body layout depends on panel model; if the OMNI_PRO_II walker
        # misaligns for another model, leave the connection fields unset
        # rather than raising. The header is still useful on its own.
        return account

    account.network_address = network_address
    account.network_port = network_port
    account.controller_key = controller_key
    account.programs = programs
    account.zone_names = walk.zone_names
    account.unit_names = walk.unit_names
    account.button_names = walk.button_names
    account.code_names = walk.code_names
    account.thermostat_names = walk.thermostat_names
    account.area_names = walk.area_names
    account.message_names = walk.message_names
    account.zone_types = walk.zone_types
    account.zone_areas = walk.zone_areas
    account.area_entry_delays = walk.area_entry_delays
    account.area_exit_delays = walk.area_exit_delays
    account.area_entry_chime = walk.area_entry_chime
    account.area_quick_arm = walk.area_quick_arm
    account.area_auto_bypass = walk.area_auto_bypass
    account.area_all_on_for_alarm = walk.area_all_on_for_alarm
    account.area_trouble_beep = walk.area_trouble_beep
    account.area_perimeter_chime = walk.area_perimeter_chime
    account.area_audible_exit_delay = walk.area_audible_exit_delay
    account.unit_types = walk.unit_types
    account.unit_areas = walk.unit_areas
    account.code_pins = walk.code_pins
    account.code_authority = walk.code_authority
    account.code_areas = walk.code_areas
    account.house_code_formats = walk.house_code_formats
    account.time_clocks = walk.time_clocks
    account.installer_code = walk.installer_code
    account.enable_pc_access = walk.enable_pc_access
    account.pc_access_code = walk.pc_access_code
    account.latitude = walk.latitude
    account.longitude = walk.longitude
    account.time_zone = walk.time_zone
    account.dst_start_month = walk.dst_start_month
    account.dst_start_week = walk.dst_start_week
    account.dst_end_month = walk.dst_end_month
    account.dst_end_week = walk.dst_end_week
    account.temp_format = walk.temp_format
    account.num_areas_used = walk.num_areas_used

    # PCA03+ continues past Connection with ModemBaud flags +
    # AccountRemarks_Extended + nine Description blocks + the Remarks
    # table. We walk it on a best-effort basis — a failure here leaves
    # the post-Connection fields empty without affecting the Connection
    # fields above.
    if file_version >= 3:
        rwalk = _walk_to_remarks(r)
        account.account_remarks_extended = rwalk.account_remarks_extended
        account.zone_descriptions = rwalk.zone_descriptions
        account.unit_descriptions = rwalk.unit_descriptions
        account.button_descriptions = rwalk.button_descriptions
        account.code_descriptions = rwalk.code_descriptions
        account.thermostat_descriptions = rwalk.thermostat_descriptions
        account.area_descriptions = rwalk.area_descriptions
        account.message_descriptions = rwalk.message_descriptions
        account.audio_source_descriptions = rwalk.audio_source_descriptions
        account.audio_zone_descriptions = rwalk.audio_zone_descriptions
        account.remarks = rwalk.remarks

    return account
