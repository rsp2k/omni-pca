"""Unit tests for omni_pca.client — typed request methods.

The fixture is a tiny in-process asyncio server that completes the
handshake then serves whichever opcode the test wants. No mock_panel
dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Awaitable, Callable

import pytest

from omni_pca.client import ObjectType, OmniClient
from omni_pca.crypto import (
    decrypt_message_payload,
    encrypt_message_payload,
)
from omni_pca.message import Message, encode_v2
from omni_pca.opcodes import OmniLink2MessageType, PacketType

from .test_connection import (  # reuse handshake helpers
    CONTROLLER_KEY,
    SESSION_ID,
    _do_full_handshake,
    _pack_header,
    _start_server,
)


def _name_field(name: str, width: int) -> bytes:
    encoded = name.encode("latin-1")
    return encoded + b"\x00" * (width - len(encoded))


def _build_system_information_payload() -> bytes:
    return bytes([16, 2, 12, 1]) + _name_field("415-555-1212", 24)


def _build_zone_properties_payload(index: int, name: str) -> bytes:
    return (
        bytes([1])
        + struct.pack(">H", index)
        + bytes([0, 0, 0, 1, 0])
        + _name_field(name, 15)
    )


async def _read_one_request(
    reader: asyncio.StreamReader, session_key: bytes
) -> tuple[int, Message]:
    """Read one OmniLink2Message packet from the client; return (seq, inner Message)."""
    header = await reader.readexactly(4)
    seq = (header[0] << 8) | header[1]
    type_byte = header[2]
    assert type_byte == int(PacketType.OmniLink2Message)
    first = await reader.readexactly(16)
    plain_first = decrypt_message_payload(first, seq, session_key)
    msg_len = plain_first[1]
    remaining_inner = msg_len + 4 - 16
    if remaining_inner <= 0:
        extra = 0
    else:
        pad = (-remaining_inner) % 16
        extra = remaining_inner + pad
    rest = await reader.readexactly(extra) if extra else b""
    full_ct = first + rest
    full_plain = decrypt_message_payload(full_ct, seq, session_key)
    inner = Message.decode(full_plain)
    return seq, inner


def _send_reply(
    writer: asyncio.StreamWriter,
    seq: int,
    opcode: OmniLink2MessageType,
    payload: bytes,
    session_key: bytes,
) -> None:
    inner = encode_v2(opcode, payload)
    ct = encrypt_message_payload(inner.encode(), seq, session_key)
    writer.write(_pack_header(seq, int(PacketType.OmniLink2Message)) + ct)


async def _serve_one_reply(
    handler_replies: dict[int, tuple[OmniLink2MessageType, bytes]],
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    """Build a handler that does the handshake then replies once per opcode received."""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            sk = await _do_full_handshake(r, w)
            for _ in range(len(handler_replies)):
                seq, inner = await _read_one_request(r, sk)
                opcode = inner.opcode
                if opcode not in handler_replies:
                    return
                reply_op, reply_payload = handler_replies[opcode]
                _send_reply(w, seq, reply_op, reply_payload, sk)
                await w.drain()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.Event().wait(), timeout=2.0)
        finally:
            w.close()

    return handler


@pytest.mark.asyncio
async def test_client_get_system_information_round_trip() -> None:
    handler = await _serve_one_reply(
        {
            int(OmniLink2MessageType.RequestSystemInformation): (
                OmniLink2MessageType.SystemInformation,
                _build_system_information_payload(),
            )
        }
    )
    server, host, port = await _start_server(handler)
    try:
        async with OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as c:
            info = await c.get_system_information()
            assert info.model_byte == 16
            assert info.model_name == "Omni Pro II"
            assert info.firmware_version == "2.12r1"
            assert info.local_phone == "415-555-1212"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_client_get_zone_properties_round_trip() -> None:
    handler = await _serve_one_reply(
        {
            int(OmniLink2MessageType.RequestProperties): (
                OmniLink2MessageType.Properties,
                _build_zone_properties_payload(7, "Front Door"),
            )
        }
    )
    server, host, port = await _start_server(handler)
    try:
        async with OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as c:
            zone = await c.get_object_properties(ObjectType.ZONE, 7)
            assert zone.index == 7
            assert zone.name == "Front Door"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_client_get_object_properties_eod_raises_value_error() -> None:
    """A ``EOD`` reply means the panel has no object at that index."""
    handler = await _serve_one_reply(
        {
            int(OmniLink2MessageType.RequestProperties): (
                OmniLink2MessageType.EOD,
                b"",
            )
        }
    )
    server, host, port = await _start_server(handler)
    try:
        async with OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as c:
            with pytest.raises(ValueError, match="no ZONE"):
                await c.get_object_properties(ObjectType.ZONE, 999)
    finally:
        server.close()
        await server.wait_closed()


# Keep `SESSION_ID` reachable so ruff doesn't complain about unused
# imports — it's used implicitly by `_do_full_handshake`.
assert SESSION_ID
