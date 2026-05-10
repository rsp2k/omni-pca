"""Inner v1/v2 message frame (payload of an encrypted/unencrypted packet).

Wire layout (non-addressable):
    ``[start_char][length][...data...][crc_lo][crc_hi]``

For v1 addressable messages (StartChar=0x5A) a single SerialAddress byte
is interleaved between start_char and length.

CRC is CRC-16/MODBUS (poly 0xA001, init 0, reflected) computed over the
length byte plus the data bytes. The 2-byte CRC trailer is **little-endian**
on the wire (CRC1 = low byte, CRC2 = high byte).

References:
    clsOmniLinkMessage.cs (lines 9, 164-186, 273-289) — frame + CRC
    clsOmniLink2Message.cs (lines 17-23) — v2 StartChar = 0x21
    clsOL2MsgLogin.cs / clsOLMsgLogin.cs — example payloads
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .opcodes import OmniLink2MessageType, OmniLinkMessageType

START_CHAR_V2 = 0x21
START_CHAR_V1_UNADDRESSED = 0x41
START_CHAR_V1_ADDRESSABLE = 0x5A

_CRC_POLY_REFLECTED = 0xA001


class MessageFormatError(ValueError):
    """The buffer is too short, has an unknown start char, or length is wrong."""


class MessageCrcError(ValueError):
    """The trailing CRC does not match the recomputed value over [length..data]."""


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS (poly 0xA001 reflected, init 0).

    Reference: clsOmniLinkMessage.cs:164-176 (_crcAddByte / _crcCalculate).
    """
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ _CRC_POLY_REFLECTED
            else:
                crc >>= 1
    return crc & 0xFFFF


@dataclass
class Message:
    """A single Omni-Link inner message (one opcode + its payload)."""

    start_char: int
    data: bytes
    serial_address: int = 0
    length: int = field(default=0)

    def __post_init__(self) -> None:
        if self.start_char not in (
            START_CHAR_V2,
            START_CHAR_V1_UNADDRESSED,
            START_CHAR_V1_ADDRESSABLE,
        ):
            raise MessageFormatError(f"unknown start_char {self.start_char:#04x}")
        if not self.data:
            raise MessageFormatError("data must contain at least the opcode byte")
        if len(self.data) > 0xFF:
            raise MessageFormatError(f"data length {len(self.data)} exceeds 255")
        # length encodes opcode + payload size; if caller didn't set it, derive it.
        if self.length == 0:
            self.length = len(self.data)
        elif self.length != len(self.data):
            raise MessageFormatError(
                f"declared length {self.length} != len(data) {len(self.data)}"
            )

    @property
    def opcode(self) -> int:
        return self.data[0]

    @property
    def payload(self) -> bytes:
        return self.data[1:]

    def encode(self) -> bytes:
        body = bytes([self.length]) + self.data
        crc = crc16_modbus(body)
        crc_lo = crc & 0xFF
        crc_hi = (crc >> 8) & 0xFF
        if self.start_char == START_CHAR_V1_ADDRESSABLE:
            return (
                bytes([self.start_char, self.serial_address & 0xFF]) + body + bytes([crc_lo, crc_hi])
            )
        return bytes([self.start_char]) + body + bytes([crc_lo, crc_hi])

    @classmethod
    def decode(cls, buf: bytes) -> Message:
        if len(buf) < 4:
            raise MessageFormatError(f"buffer too short: {len(buf)} bytes")
        start = buf[0]
        if start == START_CHAR_V1_ADDRESSABLE:
            serial = buf[1]
            length = buf[2]
            data_start = 3
        elif start in (START_CHAR_V2, START_CHAR_V1_UNADDRESSED):
            serial = 0
            length = buf[1]
            data_start = 2
        else:
            raise MessageFormatError(f"unknown start_char {start:#04x}")

        data_end = data_start + length
        if len(buf) < data_end + 2:
            raise MessageFormatError(
                f"buffer truncated: need {data_end + 2} bytes, have {len(buf)}"
            )
        data = bytes(buf[data_start:data_end])
        crc_received = buf[data_end] | (buf[data_end + 1] << 8)
        crc_expected = crc16_modbus(bytes([length]) + data)
        if crc_received != crc_expected:
            raise MessageCrcError(
                f"CRC mismatch: got {crc_received:#06x}, want {crc_expected:#06x}"
            )
        return cls(start_char=start, data=data, serial_address=serial, length=length)


def encode_v2(opcode: OmniLink2MessageType | int, payload: bytes = b"") -> Message:
    """Build a v2 (StartChar=0x21) message from an opcode + payload bytes."""
    return Message(start_char=START_CHAR_V2, data=bytes([int(opcode)]) + payload)


def encode_v1(
    opcode: OmniLinkMessageType | int,
    payload: bytes = b"",
    *,
    serial_address: int | None = None,
) -> Message:
    """Build a v1 message. If serial_address is given, the addressable form is used."""
    if serial_address is None:
        return Message(start_char=START_CHAR_V1_UNADDRESSED, data=bytes([int(opcode)]) + payload)
    return Message(
        start_char=START_CHAR_V1_ADDRESSABLE,
        data=bytes([int(opcode)]) + payload,
        serial_address=serial_address,
    )
