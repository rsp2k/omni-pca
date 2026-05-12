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
1-2         ``cond``        (BE u16; opaque this pass)
3-4         ``cond2``       (BE u16; opaque this pass)
5           ``cmd``         (``Command`` enum from
                            :mod:`omni_pca.commands`)
6           ``par``         (byte parameter)
7-8         ``pr2``         (BE u16, usually object#)
9-10        ``month, day``  (or ``day, month`` in the .pca
                            on-disk layout when ProgType==Event;
                            see "Mon/Day swap" below)
11          ``days``        (``Days`` bitmask)
12          ``hour``        (0-23)
13          ``minute``      (0-59)
==========  ==============================================

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

    The first five values are the actual stored types; values 5..10
    are connector tokens used by PC Access's program-line editor to
    string multiple records into one user-visible "line". The
    multi-record encoding is not yet reverse-engineered.
    """

    FREE = 0       # unused slot (all bytes zero)
    TIMED = 1      # fires at a specific time of day on selected days
    EVENT = 2      # fires when a panel event occurs (zone open, etc.)
    YEARLY = 3     # fires on a specific calendar date each year
    REMARK = 4     # stores a 32-bit RemarkID + remark-text association
    WHEN = 5       # connector (multi-record line, RE-pending)
    AT = 6         # connector
    EVERY = 7      # connector
    AND = 8        # connector
    OR = 9         # connector
    THEN = 10      # connector


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
        remark_id: int | None = (
            (body[1] << 24) | (body[2] << 16) | (body[3] << 8) | body[4]
        )
        cond = 0
        cond2 = 0
    else:
        remark_id = None
        cond = (body[1] << 8) | body[2]
        cond2 = (body[3] << 8) | body[4]

    cmd = body[5]
    par = body[6]
    pr2 = (body[7] << 8) | body[8]
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
        buf[1] = (p.cond >> 8) & 0xFF
        buf[2] = p.cond & 0xFF
        buf[3] = (p.cond2 >> 8) & 0xFF
        buf[4] = p.cond2 & 0xFF
    buf[5] = p.cmd & 0xFF
    buf[6] = p.par & 0xFF
    buf[7] = (p.pr2 >> 8) & 0xFF
    buf[8] = p.pr2 & 0xFF
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
