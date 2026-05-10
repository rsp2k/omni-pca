"""Unit tests for omni_pca.message."""

from __future__ import annotations

import pytest

from omni_pca.message import (
    START_CHAR_V1_ADDRESSABLE,
    START_CHAR_V1_UNADDRESSED,
    START_CHAR_V2,
    Message,
    MessageCrcError,
    MessageFormatError,
    crc16_modbus,
    encode_v1,
    encode_v2,
)
from omni_pca.opcodes import OmniLink2MessageType, OmniLinkMessageType


def _ref_crc(data: bytes) -> int:
    """Independently compute CRC-16/MODBUS using the textbook description."""
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def test_crc_matches_reference_for_empty() -> None:
    assert crc16_modbus(b"") == 0


def test_crc_pinned_value_for_v1_request_system_info() -> None:
    # length byte = 1 (opcode only), opcode byte = 0x11 (RequestSystemInformation in v1)
    body = bytes([0x01, 0x11])
    expected = _ref_crc(body)
    assert crc16_modbus(body) == expected
    # Pin a concrete number too so future changes need conscious approval.
    assert crc16_modbus(body) == 0x9CC1


def test_v2_encode_decode_roundtrip() -> None:
    msg = encode_v2(OmniLink2MessageType.RequestSystemInformation)
    wire = msg.encode()
    decoded = Message.decode(wire)
    assert decoded.start_char == START_CHAR_V2
    assert decoded.opcode == OmniLink2MessageType.RequestSystemInformation
    assert decoded.payload == b""


def test_v2_encode_with_payload() -> None:
    msg = encode_v2(OmniLink2MessageType.Command, payload=bytes([0x01, 0x02, 0x03]))
    wire = msg.encode()
    # [0x21, length=4, opcode=0x14, 0x01, 0x02, 0x03, crc_lo, crc_hi]
    assert wire[0] == 0x21
    assert wire[1] == 4
    assert wire[2] == int(OmniLink2MessageType.Command)
    decoded = Message.decode(wire)
    assert decoded.payload == bytes([0x01, 0x02, 0x03])


def test_v1_unaddressed_roundtrip() -> None:
    msg = encode_v1(OmniLinkMessageType.RequestSystemInformation)
    wire = msg.encode()
    assert wire[0] == START_CHAR_V1_UNADDRESSED
    decoded = Message.decode(wire)
    assert decoded.start_char == START_CHAR_V1_UNADDRESSED
    assert decoded.opcode == OmniLinkMessageType.RequestSystemInformation


def test_v1_addressable_roundtrip() -> None:
    msg = encode_v1(OmniLinkMessageType.RequestZoneStatus, serial_address=7)
    wire = msg.encode()
    assert wire[0] == START_CHAR_V1_ADDRESSABLE
    assert wire[1] == 7
    decoded = Message.decode(wire)
    assert decoded.serial_address == 7
    assert decoded.opcode == OmniLinkMessageType.RequestZoneStatus


def test_crc_tampering_raises() -> None:
    msg = encode_v2(OmniLink2MessageType.RequestSystemInformation)
    wire = bytearray(msg.encode())
    wire[-1] ^= 0xFF
    with pytest.raises(MessageCrcError):
        Message.decode(bytes(wire))


def test_unknown_start_char_raises() -> None:
    with pytest.raises(MessageFormatError):
        Message.decode(bytes([0xAB, 0x01, 0x02, 0x00, 0x00]))


def test_truncated_buffer_raises() -> None:
    msg = encode_v2(OmniLink2MessageType.Command, payload=b"\x01\x02\x03")
    wire = msg.encode()
    with pytest.raises(MessageFormatError):
        Message.decode(wire[:-1])


def test_empty_data_rejected_in_constructor() -> None:
    with pytest.raises(MessageFormatError):
        Message(start_char=START_CHAR_V2, data=b"")
