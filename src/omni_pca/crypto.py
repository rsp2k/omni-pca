"""Omni-Link II symmetric crypto primitives.

AES-128-ECB with PaddingMode.Zeros, the per-session key derivation that
folds the 5-byte controller nonce into the last 5 bytes of ControllerKey,
and the per-block sequence-number XOR pre-whitening that wraps every
encrypted packet payload.

References:
    clsAES.cs (lines 14-23, 39-55) — AES-128-ECB, PaddingMode.Zeros
    clsOmniLinkConnection.cs:1886-1892 — session key derivation (TCP path)
    clsOmniLinkConnection.cs:1423-1429 — same derivation (UDP path)
    clsOmniLinkConnection.cs:396-401  — encrypt-side per-block whitening
    clsOmniLinkConnection.cs:413-417  — decrypt-side per-block whitening
"""

from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BLOCK_SIZE = 16
_KEY_SIZE = 16
_SESSION_ID_SIZE = 5


def _check_key(key: bytes) -> None:
    if len(key) != _KEY_SIZE:
        raise ValueError(f"key must be {_KEY_SIZE} bytes, got {len(key)}")


class OmniCipher:
    """Thin AES-128-ECB wrapper that mirrors the C# clsAES contract.

    PaddingMode.Zeros means: the caller is responsible for zero-padding to
    a 16-byte boundary before encrypting; decryption returns whatever the
    raw 16-byte blocks decode to (no automatic strip).
    """

    __slots__ = ("_decryptor_factory", "_encryptor_factory")

    def __init__(self, key: bytes) -> None:
        _check_key(key)
        cipher = Cipher(algorithms.AES(key), modes.ECB())
        self._encryptor_factory = cipher.encryptor
        self._decryptor_factory = cipher.decryptor

    def encrypt(self, plaintext: bytes) -> bytes:
        if len(plaintext) % BLOCK_SIZE != 0:
            raise ValueError(
                f"plaintext length {len(plaintext)} is not a multiple of {BLOCK_SIZE}"
            )
        enc = self._encryptor_factory()
        return enc.update(plaintext) + enc.finalize()

    def decrypt(self, ciphertext: bytes) -> bytes:
        if len(ciphertext) % BLOCK_SIZE != 0:
            raise ValueError(
                f"ciphertext length {len(ciphertext)} is not a multiple of {BLOCK_SIZE}"
            )
        dec = self._decryptor_factory()
        return dec.update(ciphertext) + dec.finalize()


def derive_session_key(controller_key: bytes, session_id: bytes) -> bytes:
    """SessionKey = ControllerKey[0:11] || (ControllerKey[11:16] XOR SessionID[0:5]).

    Reference: clsOmniLinkConnection.cs:1886-1892.
    """
    _check_key(controller_key)
    if len(session_id) != _SESSION_ID_SIZE:
        raise ValueError(f"session_id must be {_SESSION_ID_SIZE} bytes, got {len(session_id)}")
    out = bytearray(controller_key)
    for j in range(_SESSION_ID_SIZE):
        out[11 + j] ^= session_id[j]
    return bytes(out)


def _check_block(block: bytes) -> None:
    if len(block) != BLOCK_SIZE:
        raise ValueError(f"block must be {BLOCK_SIZE} bytes, got {len(block)}")


def _check_seq(seq: int) -> None:
    if not 0 <= seq <= 0xFFFF:
        raise ValueError(f"seq out of uint16 range: {seq}")


def whiten_block(block: bytes, seq: int) -> bytes:
    """XOR the first two bytes of a 16-byte block with seq high then low byte.

    Identical operation in both directions (XOR is its own inverse), but we
    expose two names so the calling code reads as intent. This pre-whitening
    is non-public protocol behavior — third-party Omni-Link write-ups do not
    document it. Reference: clsOmniLinkConnection.cs:396-401.
    """
    _check_block(block)
    _check_seq(seq)
    hi = (seq >> 8) & 0xFF
    lo = seq & 0xFF
    return bytes([block[0] ^ hi, block[1] ^ lo, *block[2:]])


def unwhiten_block(block: bytes, seq: int) -> bytes:
    """Inverse of whiten_block. Reference: clsOmniLinkConnection.cs:413-417."""
    return whiten_block(block, seq)


def _whiten_inplace(buf: bytearray, seq: int) -> None:
    hi = (seq >> 8) & 0xFF
    lo = seq & 0xFF
    for off in range(0, len(buf), BLOCK_SIZE):
        buf[off] ^= hi
        buf[off + 1] ^= lo


def encrypt_message_payload(plaintext: bytes, seq: int, session_key: bytes) -> bytes:
    """Zero-pad to a 16-byte boundary, apply per-block whitening, AES-ECB encrypt.

    Reference: clsOmniLinkConnection.cs:374-401 (EncryptPacket).
    """
    _check_key(session_key)
    _check_seq(seq)
    pad = (-len(plaintext)) % BLOCK_SIZE
    buf = bytearray(plaintext) + bytearray(pad)
    _whiten_inplace(buf, seq)
    return OmniCipher(session_key).encrypt(bytes(buf))


def decrypt_message_payload(ciphertext: bytes, seq: int, session_key: bytes) -> bytes:
    """AES-ECB decrypt, then reverse the per-block whitening.

    Reference: clsOmniLinkConnection.cs:405-419 (DecryptPacket).
    """
    _check_key(session_key)
    _check_seq(seq)
    plain = bytearray(OmniCipher(session_key).decrypt(ciphertext))
    _whiten_inplace(plain, seq)
    return bytes(plain)
