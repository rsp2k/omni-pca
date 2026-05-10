"""Unit tests for omni_pca.packet."""

from __future__ import annotations

import pytest

from omni_pca.opcodes import PacketType
from omni_pca.packet import MIN_PACKET_BYTES, Packet


def test_encode_empty_data() -> None:
    pkt = Packet(seq=2, type=PacketType.ClientRequestNewSession)
    wire = pkt.encode()
    assert wire == bytes([0x00, 0x02, 0x01, 0x00])
    assert len(wire) == MIN_PACKET_BYTES


def test_encode_decode_roundtrip_one_byte() -> None:
    pkt = Packet(seq=0xFFFF, type=PacketType.OmniLink2Message, data=b"\x42")
    decoded = Packet.decode(pkt.encode())
    assert decoded == pkt


def test_encode_decode_roundtrip_max_data() -> None:
    payload = bytes(range(250))
    pkt = Packet(seq=1024, type=PacketType.OmniLink2Message, data=payload)
    decoded = Packet.decode(pkt.encode())
    assert decoded.data == payload
    assert decoded.seq == 1024
    assert decoded.type == PacketType.OmniLink2Message


def test_seq_out_of_range_raises() -> None:
    with pytest.raises(ValueError):
        Packet(seq=-1, type=PacketType.NoMessage)
    with pytest.raises(ValueError):
        Packet(seq=0x10000, type=PacketType.NoMessage)


def test_data_too_long_raises() -> None:
    with pytest.raises(ValueError):
        Packet(seq=1, type=PacketType.OmniLink2Message, data=b"\x00" * 251)


def test_decode_too_short_raises() -> None:
    with pytest.raises(ValueError):
        Packet.decode(b"\x00\x00\x00")


def test_decode_unknown_type_raises() -> None:
    # 0xFE is not a defined PacketType — IntEnum coercion should reject.
    with pytest.raises(ValueError):
        Packet.decode(bytes([0x00, 0x01, 0xFE, 0x00]))


def test_big_endian_seq() -> None:
    pkt = Packet(seq=0x1234, type=PacketType.OmniLink2Message)
    wire = pkt.encode()
    assert wire[0] == 0x12
    assert wire[1] == 0x34
