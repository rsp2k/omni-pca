"""Unit tests for omni_pca.crypto."""

from __future__ import annotations

import pytest

from omni_pca.crypto import (
    BLOCK_SIZE,
    OmniCipher,
    decrypt_message_payload,
    derive_session_key,
    encrypt_message_payload,
    unwhiten_block,
    whiten_block,
)

CONTROLLER_KEY = bytes.fromhex("6ba7b4e9b4656de3cd7edd4c650cdb09")
SESSION_ID = bytes([0x01, 0x02, 0x03, 0x04, 0x05])
# CK indices: [0..11) = "6ba7b4e9b4656de3cd7edd" verbatim.
# CK[11..16) = [0x4c, 0x65, 0x0c, 0xdb, 0x09]
# XOR with SessionID [0x01..0x05]:
#   0x4c ^ 0x01 = 0x4d
#   0x65 ^ 0x02 = 0x67
#   0x0c ^ 0x03 = 0x0f
#   0xdb ^ 0x04 = 0xdf
#   0x09 ^ 0x05 = 0x0c
EXPECTED_SESSION_KEY = bytes.fromhex("6ba7b4e9b4656de3cd7edd" + "4d670fdf0c")


def test_derive_session_key_kat() -> None:
    sk = derive_session_key(CONTROLLER_KEY, SESSION_ID)
    assert sk == EXPECTED_SESSION_KEY
    assert sk[:11] == CONTROLLER_KEY[:11]
    for i in range(5):
        assert sk[11 + i] == CONTROLLER_KEY[11 + i] ^ SESSION_ID[i]


def test_derive_session_key_rejects_bad_lengths() -> None:
    with pytest.raises(ValueError):
        derive_session_key(b"\x00" * 15, SESSION_ID)
    with pytest.raises(ValueError):
        derive_session_key(CONTROLLER_KEY, b"\x00" * 4)


@pytest.mark.parametrize("seq", [0, 1, 2, 1024, 65535])
def test_whiten_unwhiten_roundtrip(seq: int) -> None:
    block = bytes(range(BLOCK_SIZE))
    out = whiten_block(block, seq)
    assert unwhiten_block(out, seq) == block
    # First two bytes flip, others untouched.
    assert out[2:] == block[2:]
    assert out[0] == block[0] ^ ((seq >> 8) & 0xFF)
    assert out[1] == block[1] ^ (seq & 0xFF)


def test_whiten_block_bad_size() -> None:
    with pytest.raises(ValueError):
        whiten_block(b"\x00" * 15, 1)


@pytest.mark.parametrize("seq", [1, 2, 1024, 65535])
def test_encrypt_decrypt_roundtrip(seq: int) -> None:
    key = bytes(range(16))
    plain = b"hello, omni-link \x00\x01\x02"
    ct = encrypt_message_payload(plain, seq, key)
    assert len(ct) % BLOCK_SIZE == 0
    pt = decrypt_message_payload(ct, seq, key)
    # PaddingMode.Zeros: tail is the zero pad we applied to round to 16.
    assert pt.startswith(plain)
    assert all(b == 0 for b in pt[len(plain):])


def test_encrypt_zero_length_pads_to_block() -> None:
    key = bytes(range(16))
    ct = encrypt_message_payload(b"", 1, key)
    assert len(ct) == 0  # nothing to encrypt → no output


def test_wrong_key_changes_ciphertext() -> None:
    plain = b"sensitive payload"
    ct1 = encrypt_message_payload(plain, 7, bytes(range(16)))
    ct2 = encrypt_message_payload(plain, 7, bytes(range(1, 17)))
    assert ct1 != ct2


def test_omnicipher_size_validation() -> None:
    with pytest.raises(ValueError):
        OmniCipher(b"\x00" * 8)
    cipher = OmniCipher(bytes(range(16)))
    with pytest.raises(ValueError):
        cipher.encrypt(b"\x00" * 5)
    with pytest.raises(ValueError):
        cipher.decrypt(b"\x00" * 5)


def test_seq_out_of_range_rejected() -> None:
    with pytest.raises(ValueError):
        whiten_block(b"\x00" * 16, -1)
    with pytest.raises(ValueError):
        whiten_block(b"\x00" * 16, 0x10000)
    with pytest.raises(ValueError):
        encrypt_message_payload(b"a", 0x10000, bytes(range(16)))
