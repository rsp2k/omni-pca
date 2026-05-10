"""Outer-frame packet (the 4-byte header + payload that wraps everything).

Wire layout: ``[seq_hi][seq_lo][type][reserved][...data]`` with sequence
number in big-endian. The same struct carries cleartext control packets
(NewSession / AckNewSession) and AES-encrypted v1/v2 message bodies.

References:
    clsOmniLinkPacket.cs (lines 5-65) — wire format + parser
    clsOmniLinkConnection.cs:1714-1788 — receiver framing rules
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from .opcodes import PacketType

MIN_PACKET_BYTES = 4
_MAX_DATA_BYTES = 250
_HEADER_FMT = ">HBB"

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Packet:
    """An Omni-Link outer-frame packet."""

    seq: int
    type: PacketType
    reserved: int = 0
    data: bytes = b""

    def __post_init__(self) -> None:
        if not 0 <= self.seq <= 0xFFFF:
            raise ValueError(f"seq out of uint16 range: {self.seq}")
        if len(self.data) > _MAX_DATA_BYTES:
            raise ValueError(
                f"data length {len(self.data)} exceeds Omni max {_MAX_DATA_BYTES}"
            )

    def encode(self) -> bytes:
        return struct.pack(_HEADER_FMT, self.seq, int(self.type), self.reserved & 0xFF) + self.data

    @classmethod
    def decode(cls, buf: bytes) -> Packet:
        if len(buf) < MIN_PACKET_BYTES:
            raise ValueError(
                f"packet too short: {len(buf)} bytes, need at least {MIN_PACKET_BYTES}"
            )
        seq, type_byte, reserved = struct.unpack(_HEADER_FMT, buf[:MIN_PACKET_BYTES])
        if reserved != 0:
            _log.warning("packet seq=%d type=%d has non-zero reserved=%d", seq, type_byte, reserved)
        data = bytes(buf[MIN_PACKET_BYTES:])
        return cls(seq=seq, type=PacketType(type_byte), reserved=reserved, data=data)
