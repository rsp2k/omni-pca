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
    """The worked example from the docs page — TIMED program."""
    body = bytes.fromhex("018d099b094403010008 0c3e070f".replace(" ", ""))
    p = Program.from_file_record(body, slot=22)
    assert p.slot == 22
    assert p.prog_type == ProgramType.TIMED
    assert p.cond == 0x8D09
    assert p.cond2 == 0x9B09
    assert p.cmd == 0x44
    assert p.par == 3
    assert p.pr2 == 0x0100
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
