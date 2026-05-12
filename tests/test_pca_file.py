"""Unit tests for omni_pca.pca_file.

The full ``.pca`` parser is exercised via the live fixture in pca-re/extracted
which is not committed; here we validate the LCG keystream + reader plumbing
on synthesized inputs and skip the full-body test by default.
"""

from __future__ import annotations

import io
import struct

import pytest

from omni_pca.pca_file import (
    KEY_EXPORT,
    KEY_PC01,
    PcaReader,
    decrypt_pca_bytes,
    derive_key_from_stamp,
)


def _encrypt_with_lcg(plain: bytes, key: int) -> bytes:
    """Reference encryptor (mirrors decrypt_pca_bytes — same op since XOR)."""
    return decrypt_pca_bytes(plain, key)


def test_decrypt_is_xor_involution() -> None:
    plain = b"CFG05 hello world payload"
    ct = _encrypt_with_lcg(plain, 0xDEADBEEF)
    assert decrypt_pca_bytes(ct, 0xDEADBEEF) == plain


def test_keystream_never_emits_0xff() -> None:
    # The Borland LCG's `% 255` quirk means keystream values are in [0..254].
    # Encrypting a buffer of 0xFF bytes should never produce 0x00.
    ct = _encrypt_with_lcg(b"\xff" * 4096, 0x12345678)
    assert all(b != 0x00 for b in ct)


def test_keys_are_distinct() -> None:
    assert KEY_PC01 != KEY_EXPORT


def test_keys_match_decompiled_constants() -> None:
    # Pin to the literal uint values from clsPcaCfg.cs (decompiled C#).
    # Hex form of each is right next to the constant in pca_file.py.
    assert KEY_PC01 == 338847091
    assert KEY_EXPORT == 391549495


def test_derive_key_from_stamp_is_deterministic() -> None:
    a = derive_key_from_stamp("hello")
    b = derive_key_from_stamp("hello")
    assert a == b
    assert a != derive_key_from_stamp("hellp")


def test_pca_reader_basic_types() -> None:
    buf = bytes([0x05]) + struct.pack("<H", 0xBEEF) + struct.pack("<I", 0xDEADBEEF)
    r = PcaReader(buf)
    assert r.u8() == 0x05
    assert r.u16() == 0xBEEF
    assert r.u32() == 0xDEADBEEF


def test_pca_reader_string8_fixed_consumes_full_slot() -> None:
    # declared len=3, max_len=10 → consumes 1 + 10 bytes regardless
    payload = bytes([3]) + b"abc" + b"\x00" * 7
    r = PcaReader(payload + b"TAIL")
    s = r.string8_fixed(10)
    assert s == "abc"
    assert r.remaining() == b"TAIL"


def test_pca_reader_string16_fixed_consumes_full_slot() -> None:
    payload = struct.pack("<H", 2) + b"ok" + b"\x00" * 4 + b"TAIL"
    r = PcaReader(payload)
    s = r.string16_fixed(6)
    assert s == "ok"
    assert r.remaining() == b"TAIL"


def test_pca_reader_short_read_raises() -> None:
    r = PcaReader(b"\x00\x00")
    with pytest.raises(EOFError):
        r.u32()


@pytest.mark.skip(reason="needs the gitignored extracted/Our_House.pca fixture")
def test_full_pca_parse_against_real_fixture() -> None:
    # Placeholder — wire up if/when a redacted fixture lands in tests/fixtures/.
    pass


# ---- Programs block extraction against the live decrypted fixture ----
#
# These tests need the plaintext .pca dump at the path below — gitignored.
# If absent, they skip cleanly. If present, they assert the decode against
# the values established in the Phase 1 RE pass (Programs block, slot 22,
# the TIMED/EVENT/YEARLY type-distribution counts).

_FIXTURE = "/home/kdm/home-auto/HAI/pca-re/extracted/Our_House.pca.plain"


def _load_programs_blob_or_skip() -> bytes:
    from pathlib import Path

    p = Path(_FIXTURE)
    if not p.exists():
        pytest.skip(f"fixture not available: {_FIXTURE}")
    from omni_pca.pca_file import (
        _CAP_OMNI_PRO_II,
        PcaReader,
        _parse_header,
        _walk_to_connection,
    )

    r = PcaReader(p.read_bytes())
    _parse_header(r)
    return _walk_to_connection(r, _CAP_OMNI_PRO_II)


def test_programs_block_decodes_against_live_fixture() -> None:
    """All 1500 slots decode without raising; counts match Phase 1 recon."""
    from collections import Counter

    from omni_pca.programs import ProgramType, decode_program_table, iter_defined

    blob = _load_programs_blob_or_skip()
    assert len(blob) == 1500 * 14

    programs = decode_program_table(blob)
    assert len(programs) == 1500
    defined = list(iter_defined(programs))
    assert len(defined) == 330

    types = Counter(p.prog_type for p in defined)
    assert types[int(ProgramType.TIMED)] == 209
    assert types[int(ProgramType.EVENT)] == 105
    assert types[int(ProgramType.YEARLY)] == 16


def test_programs_block_round_trips_byte_for_byte() -> None:
    """The strongest correctness signal: decode → encode → compare.

    If a single byte of the 21,000-byte blob is off, this test catches it.
    """
    from omni_pca.programs import decode_program_table

    blob = _load_programs_blob_or_skip()
    programs = decode_program_table(blob)
    rebuilt = b"".join(p.encode_file_record() for p in programs)
    assert rebuilt == blob


def test_programs_sanity_invariants() -> None:
    """Coarse invariants on the 330 defined programs.

    The byte-for-byte round-trip test above is the load-bearing
    correctness signal. This adds light coverage to catch a Mon/Day
    swap regression specifically on YEARLY-typed programs (which use
    bytes 9/10 as a true calendar date).

    What we DON'T assert and why:

    * **EVENT** programs encode a u16 event identifier in bytes 9/10
      (see ``clsProgram.Evt`` at lines 152-163), not a calendar date.
    * **TIMED** programs use bytes 12/13 either as absolute hour:minute
      (0-23 : 0-59) *or* as a sunrise/sunset-relative offset
      (Owner's Manual: ±0-120 minutes), with a flag we haven't
      reverse-engineered yet. So hour=26 / minute=246 are valid wire
      values in the absence of that flag decoder.
    """
    from omni_pca.programs import ProgramType, decode_program_table, iter_defined

    blob = _load_programs_blob_or_skip()
    programs = decode_program_table(blob)
    defined = list(iter_defined(programs))

    yearly = [p for p in defined if p.prog_type == int(ProgramType.YEARLY)]
    assert yearly, "fixture should have YEARLY programs"
    for p in yearly:
        assert 1 <= p.month <= 12, (
            f"slot {p.slot} YEARLY: month={p.month}"
        )
        assert 1 <= p.day <= 31, (
            f"slot {p.slot} YEARLY: day={p.day}"
        )


def test_pca_account_dataclass_has_programs_field() -> None:
    """``PcaAccount`` exposes ``programs`` with the expected type + default.

    Verifies the API surface without needing a working .pca decrypt
    key — the integration from raw blob through ``decode_program_table``
    is covered by the other three live-fixture tests above.
    """
    from omni_pca.pca_file import PcaAccount

    fields = {f.name: f for f in PcaAccount.__dataclass_fields__.values()}
    assert "programs" in fields
    # default_factory or default — the field should be an empty tuple
    # when no programs are decoded.
    inst = PcaAccount(
        version_tag="PCA03", file_version=3,
        model=16, firmware_major=2, firmware_minor=12, firmware_revision=1,
    )
    assert inst.programs == ()


def test_pca_reader_io_state_introspection() -> None:
    r = PcaReader(b"abcdef")
    assert isinstance(r.buf, io.BytesIO)
    r.bytes_(2)
    assert r.position() == 2
