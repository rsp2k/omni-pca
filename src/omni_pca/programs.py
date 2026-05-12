"""Decoder/encoder for the Omni Pro II program record.

A "program" is one line in the panel's built-in automation engine —
the panel-side counterpart to a Home Assistant automation. Each panel
stores up to 1500 programs in a fixed-size table: in a .pca file
they live in a 21,000-byte block (1500 × 14 bytes); on the wire they
are exchanged one at a time via :class:`clsOLMsgProgramData` /
:class:`clsOL2MsgProgramData`.

The Omni Pro II has the ``DoubleProgramConditional`` feature flag
set, so each record is **14 bytes** with two condition slots:

==========  ==============================================
Offset      Field
==========  ==============================================
0           ``prog_type``  (``ProgramType`` enum)
1-2         ``cond``        (LE u16; first AND-IF condition)
3-4         ``cond2``       (LE u16; second AND-IF condition)
5           ``cmd``         (``Command`` enum from
                            :mod:`omni_pca.commands`)
6           ``par``         (byte parameter)
7-8         ``pr2``         (LE u16, usually object#)
9-10        ``month, day``  (or ``day, month`` in the .pca
                            on-disk layout when ProgType==Event;
                            see "Mon/Day swap" below)
11          ``days``        (``Days`` bitmask)
12          ``hour``        (0-23)
13          ``minute``      (0-59)
==========  ==============================================

**Byte order:** all 16-bit fields above are **little-endian**
(byte N is the low byte, byte N+1 is the high byte). This was
empirically confirmed against PC Access — see findings notes
in ``pca-re/clausal-re/FINDINGS.md``. Older versions of this
module decoded them as BE; the LE encoding is correct.

When ``prog_type == Remark`` (4), bytes 1-4 hold a 32-bit BE
RemarkID instead of cond/cond2; the lookup table that resolves an
ID back to the user-visible remark text lives elsewhere on disk
and is not implemented yet.

**Mon/Day swap (a quirk worth knowing about):**
There are two byte layouts in the wild for the same Program record.

* **Wire layout** — ``clsOLMsgProgramData`` and
  ``clsProgram.ToByteArray()``: bytes at offsets 9/10 are always
  ``[month, day]``, regardless of program type. This is what the
  panel sends over UDP/TCP.
* **File layout** — what ``clsProgram.Read/Write`` writes into a
  ``.pca`` file: same layout *except* for ``prog_type == Event``,
  where bytes 9/10 are swapped to ``[day, month]``.

Our :class:`Program` dataclass normalises both to semantic fields
(``month`` and ``day`` always mean what their names say). Use
:meth:`Program.from_wire_bytes` / :meth:`encode_wire_bytes` for
on-the-wire messages and :meth:`Program.from_file_record` /
:meth:`encode_file_record` for ``.pca`` table slots. The split is
load-bearing for round-trip stability.

What this module deliberately does NOT do (yet):

* Decode the internal bit-split of ``cond`` / ``cond2`` into
  selector + operand (zone#, security mode, time clock, etc.).
* Recognise the When/At/Every/And/Or/Then connector ProgTypes that
  string multiple records into one user-visible "program line" —
  none appear in any fixture we have, so the multi-record encoding
  is still un-RE'd.
* Resolve RemarkID → RemarkText (the lookup table is on a TODO).

References:
    clsProgram.cs (entire file) — field accessors, Read/Write,
        ToByteArray/FromByteArray, Mon/Day swap for Event-typed
        programs at lines 471-484 and 506-515.
    enuProgramType.cs — the program-type enum mirrored below.
    enuProgramCond.cs — the condition-family enum.
    enuDays.cs — day-of-week bitmask.
    Installation Manual *INSTALLER SETUP → SETUP MISC* (Programs)
    and Owner's Manual *Programming* chapter for the user-visible
    model.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

PROGRAM_BYTES = 14
"""On-disk and on-wire size of one program record on the Omni Pro II.

If we ever support models without ``DoubleProgramConditional``, that
value drops to 12 — see the docstring up top for the layout
difference.
"""

MAX_PROGRAMS = 1500
"""Number of program slots per panel (Omni Pro II)."""


class ProgramType(IntEnum):
    """Program record discriminator (``enuProgramType``).

    The 11 values split into two encoding families. Which family a
    block uses depends on the panel firmware version
    (see :data:`MIN_FIRMWARE_MULTILINE_PROGRAMS`) — **older firmware
    can only express the compact family**:

    * **Compact** (``FREE`` / ``TIMED`` / ``EVENT`` / ``YEARLY`` /
      ``REMARK``, values 0-4): always available. The whole user-visible
      block (1 trigger + up to 2 AND conditions + 1 action) fits in one
      14-byte record. The trigger discriminates the form; cmd/par/pr2
      carry the inline action; cond/cond2 carry up to two AND-IF
      conditions. PC Access calls this a "simple program".
    * **Multi-record** (``WHEN`` / ``AT`` / ``EVERY`` / ``AND`` / ``OR``
      / ``THEN``, values 5-10): one record per "line" in the block.
      Used when the block would need 3+ conditions, an OR alternative,
      or a comment block — anything the compact form can't express.
      Requires the ``MultiLinePrograms`` capability flag on the panel,
      which on the OmniPro II is gated to firmware ≥3.0.0
      (clsCapOMNI_PRO_II.cs:290 — ``Features.Add(MultiLinePrograms,
      196608u)``). On firmware <3.0 these ProgType values simply
      cannot appear; PC Access's "Or" button and "Add Comment Block"
      menu item are disabled.

    Reference:
        clsAutomationBlock.cs BuildLines() lines 80-131 for the
        compact-vs-multi rendering; clsCapOMNI_PRO_II.cs:290 for the
        firmware gate; frmAutomationEditBlock.cs:809-823
        (``MustBeSimpleProgram()``) for the toolbar enable logic.
    """

    FREE = 0       # unused slot (all bytes zero)
    TIMED = 1      # compact: time-of-day trigger + inline action
    EVENT = 2      # compact: panel event (zone, security, etc.) + action
    YEARLY = 3     # compact: yearly date trigger + inline action
    REMARK = 4     # stores a 32-bit RemarkID + remark-text association
    WHEN = 5       # multi-record: event-trigger record (firmware ≥3.0.0)
    AT = 6         # multi-record: time-trigger record (firmware ≥3.0.0)
    EVERY = 7      # multi-record: recurring-trigger record (firmware ≥3.0.0)
    AND = 8        # multi-record: AND-condition record (firmware ≥3.0.0)
    OR = 9         # multi-record: OR-alternative record (firmware ≥3.0.0)
    THEN = 10      # multi-record: action record (firmware ≥3.0.0)


def pack_firmware_version(major: int, minor: int, revision: int = 0) -> int:
    """Pack a firmware version into HAI's 24-bit comparison form.

    HAI's capability tables compare against a single u32 packed as
    ``major * 65536 + minor * 256 + revision`` (clsHAC.FW vs the
    second arg of ``Features.Add``). The constants in this module
    use this same packing so callers can compare directly:

    >>> pack_firmware_version(2, 16, 1)
    135169
    >>> pack_firmware_version(3, 0, 0)
    196608
    """
    return (major & 0xFF) << 16 | (minor & 0xFF) << 8 | (revision & 0xFF)


MIN_FIRMWARE_MULTILINE_PROGRAMS = 196608  # 3.0.0
"""Earliest OmniPro II firmware that supports multi-record programs.

Below this version, ProgType values 5-10 (``WHEN`` / ``AT`` / ``EVERY``
/ ``AND`` / ``OR`` / ``THEN``) cannot be produced by PC Access and
will not appear in the panel's program table. The user-visible
limitation: any block that would need three or more AND-IF conditions,
or any ``Or`` alternative, can't be authored. Compact-form blocks
(values 0-4, with up to 2 inline cond/cond2 AND conditions) remain
available.

Mirrors ``Features.Add(enuFeature.MultiLinePrograms, 196608u)`` in
clsCapOMNI_PRO_II.cs:290.
"""

MIN_FIRMWARE_DOUBLE_PROGRAM_CONDITIONAL = 0  # always
"""Earliest firmware that supports two inline AND conditions
(``cond`` AND ``cond2`` together) per compact program record.

For the OmniPro II this is always on (no version gate in
clsCapOMNI_PRO_II.cs:265 — ``Features.Add(DoubleProgramConditional)``
with no version arg). The 14-byte ``PROGRAM_BYTES`` constant assumes
this feature: on models without DPC the record would be 12 bytes
and ``cond2`` would not exist.
"""


# ---- Multi-record (firmware ≥3.0.0) AND-record companion enums ------
#
# When PC Access emits a block in multi-record form (one 14-byte record
# per visual line), an AND record (ProgType=8) carries a *structured*
# condition. The byte layout per ``clsProgram.cs:326-436`` is:
#
#   byte 0      : prog_type (= 8)
#   byte 1      : OP             (enuCondOP)
#   byte 2      : Arg1_ArgType   (enuCondArgType)
#   bytes 3-4   : Arg1_IX        (u16; disk byte order — see note below)
#   byte 5      : Arg1_Field     (per-type sub-field enum)
#   byte 6      : Arg2_ArgType   (enuCondArgType)
#   bytes 7-8   : Arg2_IX        (u16)
#   byte 9      : Arg2_Field
#   bytes 10-11 : CompConst      (u16; constant operand for comparison ops)
#   bytes 12-13 : (unused)
#
# Special case: when OP == Arg1_Traditional (=0), the AND record's
# condition is rendered from ``Cond`` (bytes 1-2 as u16) using the same
# per-family scheme as compact-form ``cond`` — see
# ``clsText.cs:2281-2284 GetComplexConditionText``. The richer
# ``Arg1_*`` / ``Arg2_*`` / ``CompConst`` fields are only used when
# ``OP > 0``.
#
# **Open: disk byte order for the three u16 fields.** ``clsProgram.Read``
# reads u16s little-endian via ``clsPcaCryptFileStream.ReadUInt16``
# (clsPcaCryptFileStream.cs:159-164), but the accessors index ``Data[]``
# big-endian (``(Data[3] << 8) + Data[4]``). The single captured
# example (``AND IF NEVER`` → all zero except byte 4 = 0x01) is
# symmetric and doesn't disambiguate. Resolution requires a controlled
# capture of ``AND IF ZONE 5 SECURE`` (or similar asymmetric ix value).
# Until then we don't expose a structured ``AndRecord`` decoder — the
# raw 14 bytes are still accessible via ``Program.raw`` for callers
# who need them.


class CondOP(IntEnum):
    """``enuCondOP`` — comparison operator byte of an AND record (byte 1).

    Reference: ``HAI_Shared/enuCondOP.cs``.
    """

    ARG1_TRADITIONAL = 0  # cond u16 (bytes 1-2) carries the condition
    ARG1_EQ_ARG2 = 1
    ARG1_NE_ARG2 = 2
    ARG1_LT_ARG2 = 3
    ARG1_GT_ARG2 = 4
    ARG1_ODD = 5
    ARG1_EVEN = 6
    ARG1_MULTIPLE_ARG2 = 7
    ARG1_IN_ARG2 = 8
    ARG1_NOT_IN_ARG2 = 9


class CondArgType(IntEnum):
    """``enuCondArgType`` — type of an Arg1/Arg2 reference in an AND record.

    The Arg1_ArgType byte (byte 2) and Arg2_ArgType byte (byte 6) take
    these values. ``Constant=0`` means the corresponding ``Arg*_IX`` is
    a literal integer (e.g. a temperature setpoint) rather than an
    object reference.

    Reference: ``HAI_Shared/enuCondArgType.cs``.
    """

    CONSTANT = 0
    USER_SETTING = 1
    ZONE = 2
    UNIT = 3
    THERMOSTAT = 4
    AUXILLARY = 5  # sic (spelling matches HAI source)
    AREA = 6
    TIME_DATE = 7
    AUDIO = 8
    ACCESS_CONTROL = 9
    MESSAGE = 10
    SYSTEM = 11


class ProgramCond(IntEnum):
    """Condition family byte (``enuProgramCond``).

    The high bits of ``cond`` / ``cond2`` discriminate the family;
    the low bits carry the selector / operand. We expose the family
    enum here but do **not** decode the bit split — that's a future
    pass once we can drive PC Access with controlled inputs.
    """

    OTHER = 0
    ZONE = 4
    CTRL = 8
    TIME = 12
    SEC = 16


class Days(IntFlag):
    """Day-of-week bitmask (``enuDays``).

    Note: Sunday is the high bit (0x80), not 0x01 — and Monday is
    0x02, not 0x01. ``enuDays.None`` (zero) means "no day selected".
    """

    NONE = 0
    MONDAY = 0x02
    TUESDAY = 0x04
    WEDNESDAY = 0x08
    THURSDAY = 0x10
    FRIDAY = 0x20
    SATURDAY = 0x40
    SUNDAY = 0x80


class TimeKind(IntEnum):
    """How the ``hour`` / ``minute`` bytes of a TIMED program are interpreted.

    PC Access overloads the ``Hr`` byte as a one-of-three discriminator:
    a value in 0..23 means an absolute wall-clock time; ``Hr == 25``
    means sunrise-relative; ``Hr == 26`` means sunset-relative. For
    the two relative kinds, ``Min`` is read as a **signed** byte
    (-128..127): a positive value is minutes *after* sunrise/sunset,
    a negative value is minutes *before*, and zero is "at".

    Reference: frmPopUpEditTime.cs:186-217 (decode), :241-263 (encode).
    """

    ABSOLUTE = 0
    SUNRISE = 1
    SUNSET = 2


_HR_SUNRISE_SENTINEL = 25
_HR_SUNSET_SENTINEL = 26


class ConditionFamily(IntEnum):
    """Top-level discriminator for the 16-bit ``cond`` / ``cond2`` field.

    Found by ``(cond >> 8) & 0xFC`` — i.e. the high byte's bits 2-7
    (clsText.cs:2226). The four explicit families match
    :class:`ProgramCond`; ``SEC`` is the catch-all default that
    handles security-mode conditions (and anything else that doesn't
    match the first four).
    """

    OTHER = 0
    ZONE = 4
    CTRL = 8
    TIME = 12
    SEC = 16


class MiscConditional(IntEnum):
    """Misc-conditional enum (``enuMiscConditional``) used by the
    :attr:`ConditionFamily.OTHER` family.

    Low 4 bits of ``cond`` index into this table; the high bits are zero.
    """

    NONE = 0
    NEVER = 1
    LIGHT = 2
    DARK = 3
    PHONE_DEAD = 4
    PHONE_RINGING = 5
    PHONE_OFF_HOOK = 6
    PHONE_ON_HOOK = 7
    AC_POWER_OFF = 8
    AC_POWER_ON = 9
    BATTERY_LOW = 10
    BATTERY_OK = 11
    ENERGY_COST_LOW = 12
    ENERGY_COST_MID = 13
    ENERGY_COST_HIGH = 14
    ENERGY_COST_CRITICAL = 15


@dataclass(frozen=True, slots=True)
class Condition:
    """One decoded program condition (``cond`` or ``cond2`` field).

    Format per family (clsText.GetConditionalText, clsText.cs:2224-2273
    and frmAutomationEditCondition.cs):

    * ``OTHER`` (``cond < 0x400``): bits 0-3 = :class:`MiscConditional`
      value (e.g. ``DARK``, ``AC_POWER_OFF``).
    * ``ZONE`` (``cond high-byte``-bits-2-7 ``== 0x04``): bits 0-7 =
      zone number; bit 9 = ``0`` for SECURE, ``1`` for NOT_READY.
    * ``CTRL`` (``... == 0x08``): bits 0-8 = unit number (9 bits); bit 9
      = ``0`` for OFF/DOWN, ``1`` for ON/UP.
    * ``TIME`` (``... == 0x0C``): bits 0-7 = time-clock number; bit 9 =
      ``0`` for DISABLED, ``1`` for ENABLED.
    * ``SEC`` (any other value, including ``... == 0x10``): bits 8-11 =
      area number; bits 12-14 = :class:`SecurityMode` value; bit 15 =
      "arming-transition" flag (or "Lumina setting" on Lumina firmware).

    A ``cond`` of ``0`` is the "no condition" sentinel — the program
    always fires regardless of state.
    """

    raw: int
    family: ConditionFamily
    selector: int            # zone# / unit# / clock# / area# / misc-id
    operand: int             # 0/1 for Zone/Ctrl/Time; mode value for Sec
    arming_transition: bool  # only meaningful for SEC family

    @classmethod
    def decode(cls, cond: int) -> Condition:
        """Decode a 16-bit ``cond`` value into its semantic parts."""
        if not 0 <= cond <= 0xFFFF:
            raise ValueError(f"cond out of u16 range: {cond}")
        fam_byte = (cond >> 8) & 0xFC
        if fam_byte == ConditionFamily.OTHER:
            return cls(
                raw=cond,
                family=ConditionFamily.OTHER,
                selector=cond & 0x0F,
                operand=0,
                arming_transition=False,
            )
        if fam_byte == ConditionFamily.ZONE:
            return cls(
                raw=cond,
                family=ConditionFamily.ZONE,
                selector=cond & 0xFF,
                operand=(cond >> 9) & 1,
                arming_transition=False,
            )
        if fam_byte == ConditionFamily.CTRL:
            return cls(
                raw=cond,
                family=ConditionFamily.CTRL,
                selector=cond & 0x1FF,
                operand=(cond >> 9) & 1,
                arming_transition=False,
            )
        if fam_byte == ConditionFamily.TIME:
            return cls(
                raw=cond,
                family=ConditionFamily.TIME,
                selector=cond & 0xFF,
                operand=(cond >> 9) & 1,
                arming_transition=False,
            )
        # Default: SEC. Bit 15 is "arming" flag iff bits 12-14 (mode) are
        # non-zero -- otherwise it's just the mode-Off encoding marker.
        mode = (cond >> 12) & 0x7
        bit15 = (cond >> 15) & 1
        return cls(
            raw=cond,
            family=ConditionFamily.SEC,
            selector=(cond >> 8) & 0x0F,
            operand=mode,
            arming_transition=bool(bit15 and mode != 0),
        )

    def is_empty(self) -> bool:
        """``True`` when the condition field is zero — no condition applies."""
        return self.raw == 0

    def describe(self) -> str:
        """Human-readable description without name lookups.

        Renders objects by index (``"Zone 5"``, ``"Unit 12"``) since
        this dataclass doesn't carry the panel name tables. For
        installation-name resolution use :func:`format_condition`
        below with a name dict.
        """
        if self.is_empty():
            return "(no condition)"
        if self.family is ConditionFamily.OTHER:
            try:
                return MiscConditional(self.selector).name
            except ValueError:
                return f"OTHER({self.selector})"
        if self.family is ConditionFamily.ZONE:
            verb = "NOT_READY" if self.operand else "SECURE"
            return f"Zone {self.selector} {verb}"
        if self.family is ConditionFamily.CTRL:
            verb = "ON" if self.operand else "OFF"
            return f"Unit {self.selector} {verb}"
        if self.family is ConditionFamily.TIME:
            verb = "ENABLED" if self.operand else "DISABLED"
            return f"Time clock {self.selector} {verb}"
        # SEC
        from .models import SecurityMode  # local import to keep top circular-free
        try:
            mode_name = SecurityMode(self.operand).name
        except ValueError:
            mode_name = f"MODE({self.operand})"
        area = f"Area {self.selector}" if self.selector else "(any area)"
        if self.arming_transition:
            return f"{area} ARMING {mode_name}"
        return f"{area} {mode_name}"


def _classify_time(hour: int, minute: int) -> tuple[TimeKind, int]:
    """Decode ``(hour, minute)`` bytes into a ``(kind, value)`` pair.

    For ``TimeKind.ABSOLUTE`` the ``value`` is the minute byte 0..59
    (caller should also use the ``hour`` field for the full time). For
    sunrise / sunset, ``value`` is the signed minutes offset.
    """
    if hour == _HR_SUNRISE_SENTINEL:
        offset = minute if minute < 0x80 else minute - 0x100
        return TimeKind.SUNRISE, offset
    if hour == _HR_SUNSET_SENTINEL:
        offset = minute if minute < 0x80 else minute - 0x100
        return TimeKind.SUNSET, offset
    return TimeKind.ABSOLUTE, minute & 0xFF


# Once-per-process warnings — see _warn_unknown.
_warned_unknown: set[tuple[str, int]] = set()


def _warn_unknown(category: str, value: int) -> None:
    """Emit a one-time UserWarning for an unrecognised enum value.

    We pass unknown bytes through as raw ints (forward-compatibility
    for new ProgType / Cmd values we haven't catalogued yet) but
    warn once per ``(category, value)`` pair so users notice.
    """
    key = (category, value)
    if key in _warned_unknown:
        return
    _warned_unknown.add(key)
    warnings.warn(
        f"unknown {category} byte {value:#04x}; passing through as raw int",
        stacklevel=3,
    )


def _decode_common(body: bytes) -> dict[str, object]:
    """Decode the fields that don't depend on the file-vs-wire layout."""
    if len(body) != PROGRAM_BYTES:
        raise ValueError(
            f"program record must be {PROGRAM_BYTES} bytes, got {len(body)}"
        )

    prog_type = body[0]
    try:
        ProgramType(prog_type)
    except ValueError:
        _warn_unknown("ProgramType", prog_type)

    if prog_type == ProgramType.REMARK:
        # bytes 1-4 are a single BE u32 RemarkID instead of cond/cond2.
        # (RemarkID is the one BE field — cond/cond2/pr2 are LE.)
        remark_id: int | None = (
            (body[1] << 24) | (body[2] << 16) | (body[3] << 8) | body[4]
        )
        cond = 0
        cond2 = 0
    else:
        remark_id = None
        # cond, cond2, pr2 are little-endian u16 — empirically confirmed
        # by authoring known programs in PC Access and diffing bytes.
        cond = (body[2] << 8) | body[1]
        cond2 = (body[4] << 8) | body[3]

    cmd = body[5]
    par = body[6]
    pr2 = (body[8] << 8) | body[7]
    days = body[11]
    hour = body[12]
    minute = body[13]
    return {
        "prog_type": prog_type,
        "cond": cond,
        "cond2": cond2,
        "cmd": cmd,
        "par": par,
        "pr2": pr2,
        "days": days,
        "hour": hour,
        "minute": minute,
        "remark_id": remark_id,
    }


def _encode_common(p: Program) -> bytearray:
    """Encode the layout-independent fields into a fresh 14-byte buffer.

    Bytes 9 and 10 (month/day) are left zero — the layout-specific
    encoder fills them in.
    """
    buf = bytearray(PROGRAM_BYTES)
    buf[0] = p.prog_type & 0xFF
    if p.prog_type == ProgramType.REMARK and p.remark_id is not None:
        rid = p.remark_id & 0xFFFFFFFF
        buf[1] = (rid >> 24) & 0xFF
        buf[2] = (rid >> 16) & 0xFF
        buf[3] = (rid >> 8) & 0xFF
        buf[4] = rid & 0xFF
    else:
        # cond, cond2, pr2 are little-endian — see _decode_common
        buf[1] = p.cond & 0xFF
        buf[2] = (p.cond >> 8) & 0xFF
        buf[3] = p.cond2 & 0xFF
        buf[4] = (p.cond2 >> 8) & 0xFF
    buf[5] = p.cmd & 0xFF
    buf[6] = p.par & 0xFF
    buf[7] = p.pr2 & 0xFF
    buf[8] = (p.pr2 >> 8) & 0xFF
    # 9, 10 filled by encode_{wire,file}_bytes
    buf[11] = p.days & 0xFF
    buf[12] = p.hour & 0xFF
    buf[13] = p.minute & 0xFF
    return buf


@dataclass(frozen=True, slots=True)
class Program:
    """One programming line, decoded into semantic fields.

    Field semantics deliberately match the C# accessor names on
    ``clsProgram`` so cross-referencing the reverse-engineered source
    is mechanical.

    ``slot`` is the table index (1-based to match PC Access's
    "program number") when the record came from a .pca file or a
    wire ``ProgramData`` reply. ``None`` for hand-built Programs.

    ``remark_id`` is set only when ``prog_type == REMARK``; ``cond``
    and ``cond2`` are then zeroed in the dataclass (the 32-bit ID
    lives in those wire bytes instead).

    **A note on ``month`` / ``day`` for EVENT programs:** for the
    YEARLY and TIMED program types, ``month`` and ``day`` carry their
    obvious calendar semantics — and the file decoder applies the
    Mon/Day byte swap so the field values are always semantically
    correct. For EVENT programs (``prog_type == EVENT``), the two
    bytes at offsets 9-10 instead encode a 16-bit *event identifier*
    (see ``clsProgram.Evt`` at clsProgram.cs:152-163). The fields
    still hold the raw byte values — what would have been the
    "month" byte ends up in ``self.month`` and "day" byte in
    ``self.day`` — but they don't mean calendar month/day. Use the
    :attr:`event_id` property to read them as the intended u16.
    """

    slot: int | None = None
    prog_type: int = 0
    cond: int = 0
    cond2: int = 0
    cmd: int = 0
    par: int = 0
    pr2: int = 0
    month: int = 0
    day: int = 0
    days: int = 0
    hour: int = 0
    minute: int = 0
    remark_id: int | None = None

    # ---- decode ------------------------------------------------------

    @classmethod
    def from_wire_bytes(cls, body: bytes, *, slot: int | None = None) -> Program:
        """Decode a Program from the on-the-wire 14-byte body.

        "Wire" here means the payload that ``clsOLMsgProgramData``
        sends after its 2-byte BE ProgramNumber header — bytes 9/10
        are always ``[month, day]`` regardless of ``prog_type``.
        """
        f = _decode_common(body)
        return cls(slot=slot, month=body[9], day=body[10], **f)  # type: ignore[arg-type]

    @classmethod
    def from_file_record(cls, body: bytes, *, slot: int | None = None) -> Program:
        """Decode a Program from a ``.pca`` table slot.

        Same layout as wire form *except* when ``prog_type == EVENT``,
        in which case the on-disk bytes at offsets 9/10 are swapped
        to ``[day, month]`` (see clsProgram.Read at clsProgram.cs:471).
        We swap back so the resulting Program has ``month`` and
        ``day`` in semantic positions.
        """
        f = _decode_common(body)
        if f["prog_type"] == ProgramType.EVENT:
            month, day = body[10], body[9]
        else:
            month, day = body[9], body[10]
        return cls(slot=slot, month=month, day=day, **f)  # type: ignore[arg-type]

    # ---- encode ------------------------------------------------------

    def encode_wire_bytes(self) -> bytes:
        """Encode to the on-the-wire 14-byte body (no Mon/Day swap)."""
        buf = _encode_common(self)
        buf[9] = self.month & 0xFF
        buf[10] = self.day & 0xFF
        return bytes(buf)

    def encode_file_record(self) -> bytes:
        """Encode to the ``.pca`` 14-byte slot layout.

        Applies the Mon/Day swap for ``EVENT``-typed programs so the
        result round-trips byte-for-byte with what
        ``clsProgram.Write`` would produce.
        """
        buf = _encode_common(self)
        if self.prog_type == ProgramType.EVENT:
            buf[9] = self.day & 0xFF
            buf[10] = self.month & 0xFF
        else:
            buf[9] = self.month & 0xFF
            buf[10] = self.day & 0xFF
        return bytes(buf)

    # ---- convenience -------------------------------------------------

    @property
    def time_kind(self) -> TimeKind:
        """Classify the ``hour`` byte as absolute / sunrise / sunset.

        Only meaningful for TIMED programs; for other ``prog_type``
        values the return is still computed mechanically but has no
        semantic interpretation.
        """
        return _classify_time(self.hour, self.minute)[0]

    @property
    def time_offset_minutes(self) -> int:
        """Signed minutes-offset for sunrise/sunset-relative TIMED programs.

        Returns 0 for absolute-time programs (and for non-TIMED types,
        whose ``hour`` / ``minute`` bytes aren't time-of-day at all).
        Positive = after sunrise/sunset, negative = before, zero = at.
        """
        kind, value = _classify_time(self.hour, self.minute)
        return value if kind in (TimeKind.SUNRISE, TimeKind.SUNSET) else 0

    def format_time(self) -> str:
        """Human-readable rendering of the TIMED time-of-day.

        Examples:
            ``"07:15"`` for an absolute-time program.
            ``"at sunrise"`` for ``hour==25, minute==0``.
            ``"30 min before sunset"`` for ``hour==26, minute==226`` (sbyte -30).

        Returns the raw ``"hh:mm"`` form for non-TIMED programs even
        though it's semantically meaningless there; callers should
        check ``prog_type`` first.
        """
        kind, value = _classify_time(self.hour, self.minute)
        if kind == TimeKind.SUNRISE:
            if value == 0:
                return "at sunrise"
            return f"{abs(value)} min {'after' if value > 0 else 'before'} sunrise"
        if kind == TimeKind.SUNSET:
            if value == 0:
                return "at sunset"
            return f"{abs(value)} min {'after' if value > 0 else 'before'} sunset"
        return f"{self.hour:02d}:{self.minute:02d}"

    def condition(self) -> Condition:
        """Decode the primary ``cond`` field into a :class:`Condition`."""
        return Condition.decode(self.cond)

    def condition2(self) -> Condition:
        """Decode the secondary ``cond2`` field (DPC programs only).

        Returns a ``Condition`` with ``family == OTHER`` and
        ``selector == 0`` (i.e. ``is_empty()``) when ``cond2 == 0``,
        which is the common "no second condition" case.
        """
        return Condition.decode(self.cond2)

    @property
    def event_id(self) -> int:
        """The 16-bit event identifier (only meaningful for EVENT type).

        Composed as ``(month << 8) | day`` per ``clsProgram.Evt``. For
        non-EVENT program types this is a curiosity at best — it will
        still be a 16-bit value but the calendar fields it draws from
        carry their direct meaning instead.
        """
        return ((self.month & 0xFF) << 8) | (self.day & 0xFF)

    def is_empty(self) -> bool:
        """True iff the encoded record would be all-zero.

        Matches the panel's notion of a "free" slot. PC Access
        treats these as available for new programs.
        """
        return (
            self.prog_type == ProgramType.FREE
            and self.cond == 0
            and self.cond2 == 0
            and self.cmd == 0
            and self.par == 0
            and self.pr2 == 0
            and self.month == 0
            and self.day == 0
            and self.days == 0
            and self.hour == 0
            and self.minute == 0
            and self.remark_id is None
        )


def decode_program_table(blob: bytes) -> tuple[Program, ...]:
    """Decode a 1500-slot ``.pca`` Programs block (``21,000`` bytes).

    Each Program's ``slot`` is set to its 1-based table index — same
    convention PC Access uses in its program editor.
    """
    expected = MAX_PROGRAMS * PROGRAM_BYTES
    if len(blob) != expected:
        raise ValueError(
            f"programs block must be {expected} bytes, got {len(blob)}"
        )
    out: list[Program] = []
    for i in range(MAX_PROGRAMS):
        off = i * PROGRAM_BYTES
        record = blob[off : off + PROGRAM_BYTES]
        out.append(Program.from_file_record(record, slot=i + 1))
    return tuple(out)


def iter_defined(programs: tuple[Program, ...]):
    """Yield only non-empty programs (slots actually in use)."""
    return (p for p in programs if not p.is_empty())
