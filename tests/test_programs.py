"""Unit tests for omni_pca.programs.

Three layers of evidence (no external oracle, so we triangulate):

* **Golden bytes per ProgramType** — hand-curated byte vectors that
  exercise the layout-specific paths (Mon/Day swap for Event, Remark
  variant with RemarkID at bytes 1-4).
* **Round-trip property** — random 14-byte inputs survive
  ``decode(b).encode() == b`` for both wire and file layouts.
* **Unknown-enum tolerance** — bytes outside ``ProgramType`` /
  ``Command`` enum domains pass through without raising.
"""

from __future__ import annotations

import os
import struct
import warnings

import pytest

from omni_pca.programs import (
    MAX_PROGRAMS,
    PROGRAM_BYTES,
    Days,
    Program,
    ProgramType,
    decode_program_table,
    iter_defined,
)

# ---- golden bytes ---------------------------------------------------------


def test_timed_decodes_canonical_example() -> None:
    """The worked example from the docs page — TIMED program.

    ``cond``, ``cond2`` and ``pr2`` are **little-endian** u16 fields:
    byte N is the low byte, byte N+1 the high byte. The byte vector
    below comes from ``Our_House.pca`` slot 22 (a real TIMED program
    for an HLC scene at 07:15 weekday mornings).
    """
    body = bytes.fromhex("018d099b094403010008 0c3e070f".replace(" ", ""))
    p = Program.from_file_record(body, slot=22)
    assert p.slot == 22
    assert p.prog_type == ProgramType.TIMED
    # bytes 1,2 = [8d 09] → LE u16 = 0x098D
    assert p.cond == 0x098D
    # bytes 3,4 = [9b 09] → LE u16 = 0x099B
    assert p.cond2 == 0x099B
    assert p.cmd == 0x44
    assert p.par == 3
    # bytes 7,8 = [01 00] → LE u16 = 0x0001 (object #1)
    assert p.pr2 == 0x0001
    assert p.month == 8
    assert p.day == 12
    assert p.days == 0x3E
    assert p.hour == 7
    assert p.minute == 15
    assert p.remark_id is None
    # round-trip on file form
    assert p.encode_file_record() == body


def test_event_swaps_mon_day_on_file_layout() -> None:
    """EVENT programs store [day, month] at offsets 9/10 on disk.

    File bytes 9-10 = ``05 0c`` should decode to day=5, month=12 (not
    month=5, day=12). Encoding back must preserve the swap so the
    raw .pca slot bytes don't drift.
    """
    body = bytes.fromhex("020c04000001010000050cff070f")  # 14 bytes
    assert len(body) == 14
    p = Program.from_file_record(body, slot=1)
    assert p.prog_type == ProgramType.EVENT
    # Disk had [05, 0c] but EVENT swap means [day, mon].
    assert p.day == 5
    assert p.month == 12
    # Round-trip MUST re-apply the swap.
    assert p.encode_file_record() == body


def test_event_no_swap_on_wire_layout() -> None:
    """Same EVENT-type bytes via the wire decoder: NO swap.

    On the wire ``clsOLMsgProgramData`` always stores [month, day] at
    offsets 9/10 regardless of prog_type — only the .pca file form
    swaps. This test catches a regression where we accidentally swap
    in the wire path.
    """
    body = bytes.fromhex("020c04000001010000050cff070f")
    p = Program.from_wire_bytes(body, slot=1)
    assert p.prog_type == ProgramType.EVENT
    # Wire form: byte 9 = month, byte 10 = day. NO swap.
    assert p.month == 5
    assert p.day == 12
    assert p.encode_wire_bytes() == body


def test_yearly_program() -> None:
    """YEARLY programs use month + day fields semantically — no swap."""
    body = bytes.fromhex("03000000000100008b010a0f00000001")[:14]
    p = Program.from_file_record(body, slot=10)
    assert p.prog_type == ProgramType.YEARLY
    assert p.month == 0x01
    assert p.day == 0x0A
    assert p.encode_file_record() == body


def test_remark_uses_bytes_1_to_4_as_remark_id() -> None:
    """REMARK programs (prog_type=4) pack a 32-bit BE RemarkID into
    bytes 1-4 in place of cond + cond2."""
    remark_id = 0xDEADBEEF
    body = (
        bytes([int(ProgramType.REMARK)])
        + struct.pack(">I", remark_id)
        + bytes(9)
    )
    assert len(body) == 14
    p = Program.from_file_record(body)
    assert p.prog_type == ProgramType.REMARK
    assert p.remark_id == remark_id
    # cond / cond2 are zeroed in the dataclass — the bytes there are
    # the RemarkID, not condition fields.
    assert p.cond == 0
    assert p.cond2 == 0
    # Round-trip restores the RemarkID bytes verbatim.
    assert p.encode_file_record() == body


def test_all_zero_slot_is_empty() -> None:
    """A free slot decodes cleanly and round-trips."""
    p = Program.from_file_record(b"\x00" * 14, slot=999)
    assert p.is_empty()
    assert p.prog_type == ProgramType.FREE
    assert p.encode_file_record() == b"\x00" * 14
    assert p.encode_wire_bytes() == b"\x00" * 14


# ---- round-trip property ---------------------------------------------------


def test_random_round_trip_wire() -> None:
    """500 random 14-byte inputs: ``decode_wire → encode_wire == input``.

    The wire path is the simpler one (no Mon/Day swap), so it should
    round-trip every byte pattern losslessly.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        for _ in range(500):
            body = os.urandom(14)
            # Skip Remark inputs in this round — the dataclass discards
            # cond/cond2 for Remark types and re-derives them from
            # remark_id, but with no separate cond field we'd lose
            # bytes that happen to differ; the next test covers Remark
            # explicitly.
            if body[0] == int(ProgramType.REMARK):
                continue
            p = Program.from_wire_bytes(body)
            assert p.encode_wire_bytes() == body


def test_random_round_trip_file() -> None:
    """500 random 14-byte inputs through the file (Mon/Day swap) form."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        for _ in range(500):
            body = os.urandom(14)
            if body[0] == int(ProgramType.REMARK):
                continue
            p = Program.from_file_record(body)
            assert p.encode_file_record() == body


def test_remark_round_trip() -> None:
    """Remark variant round-trip — explicitly, with random RemarkIDs."""
    for _ in range(200):
        remark_id_bytes = os.urandom(4)
        body = (
            bytes([int(ProgramType.REMARK)])
            + remark_id_bytes
            + os.urandom(9)
        )
        p_file = Program.from_file_record(body)
        assert p_file.encode_file_record() == body
        p_wire = Program.from_wire_bytes(body)
        assert p_wire.encode_wire_bytes() == body


# ---- unknown-enum tolerance ------------------------------------------------


def test_unknown_prog_type_passes_through_with_warning() -> None:
    """Bytes outside ProgramType (0..10) decode to a raw int + warning.

    Reset the once-per-process cache first; otherwise earlier random
    round-trip tests may have already seen this value and silenced
    the warning.
    """
    import omni_pca.programs as pm
    pm._warned_unknown.clear()
    body = bytes([0x42]) + bytes(13)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        p = Program.from_wire_bytes(body)
    assert p.prog_type == 0x42
    assert p.encode_wire_bytes() == body
    assert any("ProgramType" in str(w.message) for w in caught)


def test_unknown_cmd_passes_through() -> None:
    """Unrecognised cmd bytes decode without raising."""
    body = bytes([int(ProgramType.TIMED), 0, 0, 0, 0, 0xFA, 0]) + bytes(7)
    p = Program.from_wire_bytes(body)
    assert p.cmd == 0xFA
    assert p.encode_wire_bytes() == body


# ---- table decode ----------------------------------------------------------


def test_decode_program_table_size_validation() -> None:
    with pytest.raises(ValueError, match="must be 21000 bytes"):
        decode_program_table(b"\x00" * 100)


def test_decode_program_table_round_trip_all_zero() -> None:
    """All-zero 21000-byte blob round-trips, slot numbers are 1..1500."""
    blob = b"\x00" * (MAX_PROGRAMS * PROGRAM_BYTES)
    programs = decode_program_table(blob)
    assert len(programs) == MAX_PROGRAMS
    assert programs[0].slot == 1
    assert programs[-1].slot == MAX_PROGRAMS
    assert all(p.is_empty() for p in programs)
    assert list(iter_defined(programs)) == []
    rebuilt = b"".join(p.encode_file_record() for p in programs)
    assert rebuilt == blob


def test_iter_defined_filters_empty() -> None:
    p1 = Program(prog_type=int(ProgramType.TIMED), slot=1, cmd=1, hour=8)
    p2 = Program(slot=2)  # empty
    p3 = Program(prog_type=int(ProgramType.EVENT), slot=3, cmd=2)
    defined = list(iter_defined((p1, p2, p3)))
    assert defined == [p1, p3]


# ---- Days bitmask sanity --------------------------------------------------


def test_days_bitmask_values() -> None:
    """Sanity-check the enuDays values against the C# definition."""
    assert Days.MONDAY == 0x02
    assert Days.SUNDAY == 0x80
    assert Days.MONDAY | Days.WEDNESDAY | Days.FRIDAY == 0x2A


# ---- TimeKind classification + sunrise/sunset offsets ----------------------


from omni_pca.programs import TimeKind  # noqa: E402


@pytest.mark.parametrize(
    "hour, minute, expected_kind, expected_offset, expected_label",
    [
        # Absolute times — hour 0..23, minute 0..59.
        (0, 0, TimeKind.ABSOLUTE, 0, "00:00"),
        (7, 15, TimeKind.ABSOLUTE, 0, "07:15"),
        (23, 59, TimeKind.ABSOLUTE, 0, "23:59"),
        # Sunrise-relative.
        (25, 0, TimeKind.SUNRISE, 0, "at sunrise"),
        (25, 30, TimeKind.SUNRISE, 30, "30 min after sunrise"),
        (25, 226, TimeKind.SUNRISE, -30, "30 min before sunrise"),
        (25, 255, TimeKind.SUNRISE, -1, "1 min before sunrise"),
        (25, 127, TimeKind.SUNRISE, 127, "127 min after sunrise"),
        # Sunset-relative.
        (26, 0, TimeKind.SUNSET, 0, "at sunset"),
        (26, 10, TimeKind.SUNSET, 10, "10 min after sunset"),
        (26, 246, TimeKind.SUNSET, -10, "10 min before sunset"),
        (26, 128, TimeKind.SUNSET, -128, "128 min before sunset"),
    ],
)
def test_time_kind_classification(
    hour, minute, expected_kind, expected_offset, expected_label
) -> None:
    p = Program(
        prog_type=int(ProgramType.TIMED),
        hour=hour, minute=minute, days=int(Days.MONDAY),
    )
    assert p.time_kind == expected_kind
    assert p.time_offset_minutes == expected_offset
    assert p.format_time() == expected_label


def test_time_kind_round_trip_through_wire() -> None:
    """Build a sunset-relative program, encode → decode → assert preserved."""
    p = Program(
        prog_type=int(ProgramType.TIMED),
        hour=26, minute=246,  # 10 min before sunset
        days=int(Days.FRIDAY | Days.SATURDAY),
    )
    body = p.encode_wire_bytes()
    p2 = Program.from_wire_bytes(body)
    assert p2.time_kind == TimeKind.SUNSET
    assert p2.time_offset_minutes == -10
    assert p2.format_time() == "10 min before sunset"


# ---- Condition bit-split decoder -----------------------------------------


from omni_pca.programs import (  # noqa: E402
    Condition,
    ConditionFamily,
    MiscConditional,
)


def test_condition_empty() -> None:
    """cond == 0 → no condition applies."""
    c = Condition.decode(0)
    assert c.is_empty()
    assert c.family is ConditionFamily.OTHER
    assert c.describe() == "(no condition)"


@pytest.mark.parametrize(
    "cond, family, selector, operand, expected_describe",
    [
        # OTHER family — bits 0-3 = MiscConditional value
        (0x0000, ConditionFamily.OTHER, 0,  0, "(no condition)"),
        (0x0002, ConditionFamily.OTHER, 2,  0, "LIGHT"),
        (0x0003, ConditionFamily.OTHER, 3,  0, "DARK"),
        (0x0008, ConditionFamily.OTHER, 8,  0, "AC_POWER_OFF"),
        (0x000B, ConditionFamily.OTHER, 11, 0, "BATTERY_OK"),
        (0x010B, ConditionFamily.OTHER, 11, 0, "BATTERY_OK"),  # high bits ignored
        # ZONE family — bits 0-7 = zone, bit 9 = NOT_READY (1) / SECURE (0)
        (0x0405, ConditionFamily.ZONE, 5,  0, "Zone 5 SECURE"),
        (0x0605, ConditionFamily.ZONE, 5,  1, "Zone 5 NOT_READY"),
        (0x040B, ConditionFamily.ZONE, 11, 0, "Zone 11 SECURE"),
        # CTRL family — bits 0-8 = unit, bit 9 = ON (1) / OFF (0)
        (0x0801, ConditionFamily.CTRL, 1,    0, "Unit 1 OFF"),
        (0x0A01, ConditionFamily.CTRL, 1,    1, "Unit 1 ON"),
        (0x09FF, ConditionFamily.CTRL, 0x1FF, 0, "Unit 511 OFF"),  # 9-bit unit
        # TIME family — bits 0-7 = clock, bit 9 = ENABLED (1) / DISABLED (0)
        (0x0C04, ConditionFamily.TIME, 4, 0, "Time clock 4 DISABLED"),
        (0x0E03, ConditionFamily.TIME, 3, 1, "Time clock 3 ENABLED"),
        # SEC family — bits 8-11 = area, bits 12-14 = mode, bit 15 = arming flag
        # mode=Off (0) with bit 15: encode of "area X is in mode Off"
        (0x8100, ConditionFamily.SEC, 1, 0, "Area 1 OFF"),
        (0x8800, ConditionFamily.SEC, 8, 0, "Area 8 OFF"),
        # mode=Day (1), no exit-delay flag → not arming-transition
        (0x1100, ConditionFamily.SEC, 1, 1, "Area 1 DAY"),
        # mode=Away (3), bit 15 set → ARMING (in transition)
        (0xB100, ConditionFamily.SEC, 1, 3, "Area 1 ARMING AWAY"),
        # area=0 selector → "(any area)"
        (0x9000, ConditionFamily.SEC, 0, 1, "(any area) ARMING DAY"),
    ],
)
def test_condition_decode_per_family(
    cond, family, selector, operand, expected_describe
) -> None:
    c = Condition.decode(cond)
    assert c.family == family, (
        f"cond={cond:#06x} family expected {family.name}, got {c.family.name}"
    )
    assert c.selector == selector, f"cond={cond:#06x} selector"
    assert c.operand == operand, f"cond={cond:#06x} operand"
    assert c.describe() == expected_describe


def test_condition_arming_transition_flag_only_when_mode_nonzero() -> None:
    """Bit 15 + mode=Off is the 'plain off' encoding, NOT an arming transition.

    Per clsText.cs:2263, the arming-transition branch requires
    ``(cond & 0xF000) != 0x8000``, which fails when only bit 15 is set
    (mode bits 12-14 are zero).
    """
    plain_off = Condition.decode(0x8100)
    assert plain_off.arming_transition is False
    assert plain_off.describe() == "Area 1 OFF"

    arming = Condition.decode(0xB100)  # bit 15 + mode=3 (AWAY)
    assert arming.arming_transition is True
    assert "ARMING" in arming.describe()


def test_program_condition_helpers() -> None:
    """Program.condition() / condition2() decode the raw u16 fields."""
    p = Program(
        prog_type=int(ProgramType.TIMED),
        cond=0x0605,    # Zone 5 NOT_READY
        cond2=0xB100,   # Area 1 ARMING AWAY
    )
    c1 = p.condition()
    c2 = p.condition2()
    assert c1.family is ConditionFamily.ZONE
    assert c1.selector == 5
    assert c1.describe() == "Zone 5 NOT_READY"
    assert c2.family is ConditionFamily.SEC
    assert c2.describe() == "Area 1 ARMING AWAY"


def test_condition_rejects_out_of_u16_range() -> None:
    with pytest.raises(ValueError):
        Condition.decode(-1)
    with pytest.raises(ValueError):
        Condition.decode(0x10000)


def test_misc_conditional_enum_matches_csharp() -> None:
    """enuMiscConditional values mirrored from clsText.cs."""
    assert MiscConditional.NONE == 0
    assert MiscConditional.DARK == 3
    assert MiscConditional.AC_POWER_OFF == 8
    assert MiscConditional.BATTERY_OK == 11
    assert MiscConditional.ENERGY_COST_CRITICAL == 15


# ---- multi-record (firmware ≥3.0.0) decoder properties ----------------


def test_is_multi_record_classifier() -> None:
    """Compact-form ProgTypes (0-4) are NOT multi-record; 5-10 ARE."""
    for pt in (
        ProgramType.FREE,
        ProgramType.TIMED,
        ProgramType.EVENT,
        ProgramType.YEARLY,
        ProgramType.REMARK,
    ):
        p = Program(prog_type=int(pt))
        assert not p.is_multi_record(), f"{pt.name} should NOT be multi-record"
    for pt in (
        ProgramType.WHEN,
        ProgramType.AT,
        ProgramType.EVERY,
        ProgramType.AND,
        ProgramType.OR,
        ProgramType.THEN,
    ):
        p = Program(prog_type=int(pt))
        assert p.is_multi_record(), f"{pt.name} SHOULD be multi-record"


def test_when_event_id_zone_5_secure() -> None:
    """WHEN record bytes 9-10 = (family, instance) in BE wire form.

    Empirical capture: "WHEN ZONE 5 SECURE" yields bytes 9-10 = [04, 05]
    → event_id = 0x0405 (= (ZONE=4, instance=5)).
    """
    body = bytes.fromhex("05 00 00 00 00 00 00 00 00 04 05 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=17)
    assert p.prog_type == ProgramType.WHEN
    assert p.event_id == 0x0405
    # The family code 0x04 in the high byte matches ProgramCond.ZONE
    assert (p.event_id >> 8) & 0xFC == 0x04  # ZONE family
    assert p.event_id & 0xFF == 0x05  # zone # 5


def test_when_event_id_zone_1_secure() -> None:
    """Second WHEN capture: ZONE 1 SECURE → event_id 0x0401."""
    body = bytes.fromhex("05 00 00 00 00 00 00 00 00 04 01 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=6)
    assert p.prog_type == ProgramType.WHEN
    assert p.event_id == 0x0401


def test_every_interval_5_seconds() -> None:
    """EVERY record: interval at bytes 3-4 BE.

    Empirical capture: "EVERY 5 SECONDS" trigger yields
    08 00 00 00 05 00 ... at byte positions 0-5 (ProgType=7 at byte 0,
    then zeros until byte 4 = 0x05 holding the interval low byte).
    """
    body = bytes.fromhex("07 00 00 00 05 00 00 00 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=2)
    assert p.prog_type == ProgramType.EVERY
    assert p.every_interval == 5


def test_and_unit_1_on() -> None:
    """AND IF UNIT 1 ON: byte 1 = 0x0A (CTRL family + ON bit), bytes 3-4 BE = 1.

    Empirical capture from block 9 slot 18 — the structured AND test.
    """
    body = bytes.fromhex("08 0a 00 00 01 00 00 00 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=18)
    assert p.prog_type == ProgramType.AND
    # Byte 1 = 0x0a in the high byte means CTRL family (0x08) + ON bit (0x02)
    assert p.and_family == 0x0A
    # Family code (top 6 bits): CTRL = 0x08
    assert p.and_family & 0xFC == 0x08
    # Operand bit (bit 1 of family byte = bit 9 of compact cond u16): ON
    assert p.and_family & 0x02 == 0x02
    # Instance = unit #
    assert p.and_instance == 1


def test_and_zone_5_secure() -> None:
    """AND IF ZONE 5 SECURE: byte 1 = 0x04 (ZONE + SECURE), bytes 3-4 BE = 5."""
    body = bytes.fromhex("08 04 00 00 05 00 00 00 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=7)
    assert p.prog_type == ProgramType.AND
    assert p.and_family == 0x04  # ZONE family, SECURE operand (bit 1 = 0)
    assert p.and_family & 0xFC == 0x04  # ZONE family
    assert p.and_family & 0x02 == 0  # SECURE (operand bit clear)
    assert p.and_instance == 5  # zone # 5


def test_and_never() -> None:
    """AND IF NEVER: byte 1 = 0x00 (OTHER family), bytes 3-4 BE = 1 (NEVER value)."""
    body = bytes.fromhex("08 00 00 00 01 00 00 00 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=8)
    assert p.prog_type == ProgramType.AND
    assert p.and_family == 0x00  # OTHER family
    assert p.and_instance == int(MiscConditional.NEVER)  # = 1


def test_at_record_layout() -> None:
    """AT record (multi-record TIMED): same byte layout as compact TIMED.

    Empirical capture: AT 12:01 AM all-7-days yields:
        06 00 00 00 00 00 00 00 00 05 0c fe 00 01
    Where bytes 9-10 = [05, 0c] (month=5, day=12; no Mon/Day swap
    since AT isn't EVENT-typed), byte 11 = 0xfe (Days: all 7),
    bytes 12-13 = 00:01.
    """
    body = bytes.fromhex("06 00 00 00 00 00 00 00 00 05 0c fe 00 01".replace(" ", ""))
    p = Program.from_file_record(body, slot=7)
    assert p.prog_type == ProgramType.AT
    assert p.month == 5
    assert p.day == 12
    assert p.days == 0xFE  # MTWTFSS (bit 1 through bit 7)
    assert p.hour == 0
    assert p.minute == 1


def test_or_record_is_pure_discriminator() -> None:
    """OR record: only ProgType set, all other bytes zero."""
    body = bytes.fromhex("09 00 00 00 00 00 00 00 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=10)
    assert p.prog_type == ProgramType.OR
    assert p.cond == 0
    assert p.cond2 == 0
    assert p.cmd == 0
    assert p.par == 0
    assert p.pr2 == 0
    assert p.month == 0
    assert p.day == 0
    assert p.days == 0
    assert p.hour == 0
    assert p.minute == 0


def test_then_record_uses_compact_action_layout() -> None:
    """THEN record (multi-record action): same cmd/par/pr2 layout as compact form.

    Empirical capture: THEN UNIT 1 ON yields
        0a 00 00 00 00 01 00 01 00 00 00 00 00 00
    with cmd=1 (On), par=0, pr2=1 (UNIT 1, LE).
    """
    body = bytes.fromhex("0a 00 00 00 00 01 00 01 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=10)
    assert p.prog_type == ProgramType.THEN
    assert p.cmd == 1  # enuUnitCommand.On
    assert p.par == 0
    assert p.pr2 == 1  # UNIT 1 (LE u16 at bytes 7-8, same as compact)


# ---- structured AND records (firmware ≥3.0, OP > 0) ------------------


def test_and_structured_date_eq_1231() -> None:
    """Structured AND IF DATE IS EQUAL TO 12/31 (block 12 slot 13).

    Captured bytes: 08 07 01 00 00 01 00 1f 0c 00 00 00 00 00

    Decodes per clsProgram.cs:326-436 accessors after Read's LE-to-BE
    byte swap. The OP is non-zero (Arg1_EQ_Arg2), so this is the
    "structured" case where Arg1_ArgType holds an actual enuCondArgType
    value (TimeDate=7) rather than a compact-form family code.
    """
    body = bytes.fromhex("08 07 01 00 00 01 00 1f 0c 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=13)
    assert p.prog_type == ProgramType.AND
    assert p.and_op == 1  # enuCondOP.Arg1_EQ_Arg2
    assert p.and_arg1_argtype == 7  # enuCondArgType.TimeDate
    assert p.and_instance == 0  # Arg1_IX = 0 (CURRENT_DATE)
    assert p.and_arg1_field == 1  # Date sub-field
    assert p.and_arg2_argtype == 0  # enuCondArgType.Constant
    # Arg2_IX = (month << 8) | day = (12 << 8) | 31 = 0x0c1f
    assert p.and_arg2_ix == 0x0C1F
    assert p.and_arg2_ix >> 8 == 12  # month
    assert p.and_arg2_ix & 0xFF == 31  # day
    assert p.and_arg2_field == 0
    assert p.and_compconst == 0


def test_and_traditional_zone_5_secure_via_structured_view() -> None:
    """Traditional AND (OP=0) read via the structured-AND accessors.

    For the Traditional case, Arg1_ArgType holds the compact-form
    family code (ZONE=4) — NOT the enuCondArgType Zone=2. This is the
    "dual-use byte" behavior documented at clsConditionLine.cs:17-33.
    """
    # AND IF ZONE 5 SECURE — same byte vector as earlier and_zone_5_secure test
    body = bytes.fromhex("08 04 00 00 05 00 00 00 00 00 00 00 00 00".replace(" ", ""))
    p = Program.from_file_record(body, slot=7)
    assert p.and_op == 0  # Arg1_Traditional
    # Arg1_ArgType holds the ProgramCond family code (ZONE=4), not enuCondArgType.Zone=2
    assert p.and_arg1_argtype == 4
    # and_family is the same byte for this case
    assert p.and_family == p.and_arg1_argtype
    # The instance number is still in bytes 3-4 BE
    assert p.and_instance == 5
