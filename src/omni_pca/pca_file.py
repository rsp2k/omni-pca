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
    # HouseCodes flags, and 6 TimeClock When-structs.
    area_entry_chime: dict[int, bool] = field(default_factory=dict)
    area_quick_arm: dict[int, bool] = field(default_factory=dict)
    area_auto_bypass: dict[int, bool] = field(default_factory=dict)
    area_all_on_for_alarm: dict[int, bool] = field(default_factory=dict)
    area_trouble_beep: dict[int, bool] = field(default_factory=dict)

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
    "zoneAreaOffset": 3106,
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


def _walk_to_remarks(r: PcaReader) -> dict[int, str]:
    """Pick up just-past Connection and walk through the description
    blocks, then read and return the Remarks dict.

    Layout for PCA03 (FileVersion 3, per clsHAC.cs:8055-8079):

    1. ``ModemBaud`` (u16 LE), 3× bool (1 byte each), AccountRemarks_Extended
       (String16: u16 length + bytes).
    2. Nine Description blocks, one per object family — Zones, Units,
       Buttons, Codes, Thermostats, Areas, Messages, AudioSources,
       AudioZones. Each is ``[u32 count] + count * 33 bytes`` (per
       :data:`_DESCRIPTION_SLOT_BYTES`); the contents are per-object
       free-text descriptions we don't currently surface.
    3. Remarks table:
           - ``_RemarksNextID`` (u32 LE) — what ``RemarksNextID()`` will
             hand out next.
           - ``count`` (u32 LE).
           - ``count`` entries of ``[u32 LE remark_id][String16 text]``.

    Returns ``{}`` on any read failure (panels without remarks, or a
    file format we don't recognise, just produce an empty dict —
    callers shouldn't need to special-case that).

    Reference: clsPrograms.ReadRemarks (clsPrograms.cs:148-168),
    clsAbstractNamedItem.ReadDescription, clsHAC.cs:8055-8079.
    """
    try:
        r.u16()                  # ModemBaud
        r.u8(); r.u8(); r.u8()   # PCModemInit1/2/3 enable flags
        r.string16()             # AccountRemarks_Extended (variable)
        # Nine description blocks.
        for _ in range(9):
            count = r.u32()
            if count > 0:
                r.bytes_(count * _DESCRIPTION_SLOT_BYTES)
        # Remarks table.
        r.u32()                  # _RemarksNextID
        remark_count = r.u32()
        out: dict[int, str] = {}
        for _ in range(remark_count):
            rid = r.u32()
            text = r.string16()
            out[rid] = text
        return out
    except (EOFError, ValueError, struct.error):
        return {}


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
    account.temp_format = walk.temp_format
    account.num_areas_used = walk.num_areas_used

    # PCA03+ continues past Connection with ModemBaud flags + nine
    # Description blocks + the Remarks table. We walk it on a
    # best-effort basis — a failure here leaves account.remarks={}
    # without affecting the Connection fields above.
    if file_version >= 3:
        account.remarks = _walk_to_remarks(r)

    return account
