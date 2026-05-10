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


def test_pca_reader_io_state_introspection() -> None:
    r = PcaReader(b"abcdef")
    assert isinstance(r.buf, io.BytesIO)
    r.bytes_(2)
    assert r.position() == 2
