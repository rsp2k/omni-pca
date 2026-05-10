"""Unit tests for omni_pca.connection.

These spin up tiny ``asyncio.start_server`` mock controllers inside the
test, byte-for-byte; nothing depends on a real panel or the (parallel)
mock_panel module.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Awaitable, Callable

import pytest

from omni_pca.connection import (
    ConnectionState,
    HandshakeError,
    InvalidEncryptionKeyError,
    OmniConnection,
    RequestTimeoutError,
)
from omni_pca.crypto import (
    decrypt_message_payload,
    derive_session_key,
    encrypt_message_payload,
)
from omni_pca.message import encode_v2
from omni_pca.opcodes import OmniLink2MessageType, PacketType

# A canonical 16-byte ControllerKey for tests.
CONTROLLER_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
SESSION_ID = bytes([0x10, 0x11, 0x12, 0x13, 0x14])


def _pack_header(seq: int, type_byte: int) -> bytes:
    return struct.pack(">HBB", seq, type_byte, 0)


async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
    """Read one outer-frame packet (full bytes), returning (seq, type, data).

    For the cleartext control packets we know the payload size from the
    type. For OmniLink2Message we mirror the client-side
    decrypt-first-block-to-learn-length dance.
    """
    header = await reader.readexactly(4)
    seq = (header[0] << 8) | header[1]
    type_byte = header[2]
    if type_byte == int(PacketType.ClientRequestNewSession):
        return seq, type_byte, b""
    if type_byte == int(PacketType.ClientRequestSecureSession):
        return seq, type_byte, await reader.readexactly(16)
    if type_byte == int(PacketType.ClientSessionTerminated):
        return seq, type_byte, b""
    if type_byte == int(PacketType.OmniLink2Message):
        # Read first block, decrypt to learn length.
        first = await reader.readexactly(16)
        # Caller knows the session key at this point — but for the tests
        # we just return the ciphertext + raw bytes; the test will
        # decrypt manually if it cares.
        return seq, type_byte, first
    raise AssertionError(f"unexpected client packet type {type_byte}")


# ---- handshake -----------------------------------------------------------


async def _start_server(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> tuple[asyncio.Server, str, int]:
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    sockets = server.sockets
    assert sockets, "server has no listening sockets"
    host, port = sockets[0].getsockname()[:2]
    return server, host, port


async def _do_full_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    controller_key: bytes = CONTROLLER_KEY,
    session_id: bytes = SESSION_ID,
) -> bytes:
    """Server-side: complete the 4-step handshake, return derived session key."""
    # Step 1: client sends empty ClientRequestNewSession.
    seq1, type1, _ = await _read_packet(reader)
    assert type1 == int(PacketType.ClientRequestNewSession)
    assert seq1 == 1

    # Step 2: send ControllerAckNewSession back, echoing seq=1.
    proto = bytes([0x00, 0x01])
    writer.write(_pack_header(seq1, int(PacketType.ControllerAckNewSession)) + proto + session_id)
    await writer.drain()

    # Step 3: client sends ClientRequestSecureSession (encrypted).
    seq3, type3, ct = await _read_packet(reader)
    assert type3 == int(PacketType.ClientRequestSecureSession)
    assert seq3 == 2
    session_key = derive_session_key(controller_key, session_id)
    plain = decrypt_message_payload(ct, seq3, session_key)
    assert plain[:5] == session_id

    # Step 4: send ControllerAckSecureSession back (encrypted echo).
    echo = encrypt_message_payload(session_id, seq3, session_key)
    writer.write(_pack_header(seq3, int(PacketType.ControllerAckSecureSession)) + echo)
    await writer.drain()
    return session_key


@pytest.mark.asyncio
async def test_connection_handshake_flow_with_canned_server() -> None:
    """The 4-step handshake completes; state == ONLINE; session key matches."""
    server_done = asyncio.Event()
    server_session_key: list[bytes] = []

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            sk = await _do_full_handshake(r, w)
            server_session_key.append(sk)
            server_done.set()
            # Hold the connection open so the client can close cleanly.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.Event().wait(), timeout=5.0)
        finally:
            w.close()

    server, host, port = await _start_server(handler)
    try:
        conn = OmniConnection(host=host, port=port, controller_key=CONTROLLER_KEY)
        await conn.connect()
        try:
            await asyncio.wait_for(server_done.wait(), timeout=2.0)
            assert conn.state is ConnectionState.ONLINE
            assert conn.session_key == server_session_key[0]
            assert conn.session_key == derive_session_key(CONTROLLER_KEY, SESSION_ID)
        finally:
            await conn.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_handshake_wrong_key_raises_invalid_encryption_key() -> None:
    """Server sends ControllerSessionTerminated after step 3 -> InvalidEncryptionKeyError."""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            seq1, type1, _ = await _read_packet(r)
            assert type1 == int(PacketType.ClientRequestNewSession)
            # Step 2: legitimate-looking ack.
            w.write(
                _pack_header(seq1, int(PacketType.ControllerAckNewSession))
                + bytes([0x00, 0x01])
                + SESSION_ID
            )
            await w.drain()
            # Step 3: client sends encrypted secure-session req — we read
            # and ignore, then send ControllerSessionTerminated.
            seq3, _, _ = await _read_packet(r)
            w.write(_pack_header(seq3, int(PacketType.ControllerSessionTerminated)))
            await w.drain()
        finally:
            w.close()

    server, host, port = await _start_server(handler)
    try:
        conn = OmniConnection(host=host, port=port, controller_key=CONTROLLER_KEY)
        with pytest.raises(InvalidEncryptionKeyError):
            await conn.connect()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_handshake_unsupported_proto_version_raises() -> None:
    """A non-(00,01) protocol version in step 2 produces HandshakeError."""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            seq1, _, _ = await _read_packet(r)
            w.write(
                _pack_header(seq1, int(PacketType.ControllerAckNewSession))
                + bytes([0x00, 0x02])  # wrong proto
                + SESSION_ID
            )
            await w.drain()
        finally:
            w.close()

    server, host, port = await _start_server(handler)
    try:
        conn = OmniConnection(host=host, port=port, controller_key=CONTROLLER_KEY)
        with pytest.raises(HandshakeError):
            await conn.connect()
    finally:
        server.close()
        await server.wait_closed()


# ---- sequence numbers ----------------------------------------------------


def test_sequence_number_increments_per_request() -> None:
    """Direct unit test of the seq allocator (no I/O)."""
    conn = OmniConnection("0", 1, controller_key=CONTROLLER_KEY)
    seqs = [conn._claim_seq() for _ in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


def test_sequence_number_skips_zero_on_wraparound() -> None:
    """After 0xFFFF we go to 1, not 0 (0 is reserved for unsolicited)."""
    conn = OmniConnection("0", 1, controller_key=CONTROLLER_KEY)
    conn._next_seq = 0xFFFE
    a = conn._claim_seq()
    b = conn._claim_seq()
    c = conn._claim_seq()
    assert a == 0xFFFE
    assert b == 0xFFFF
    assert c == 1


# ---- request / unsolicited dispatch --------------------------------------


@pytest.mark.asyncio
async def test_unsolicited_packet_lands_in_iterator_not_request_future() -> None:
    """An inbound packet with seq=0 goes to the unsolicited queue, not any pending future."""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            sk = await _do_full_handshake(r, w)
            # Push an unsolicited Properties-shaped message at seq=0.
            inner = encode_v2(OmniLink2MessageType.SystemEvents, b"\x00\x01")
            ct = encrypt_message_payload(inner.encode(), 0, sk)
            w.write(_pack_header(0, int(PacketType.OmniLink2Message)) + ct)
            await w.drain()
            # Hold open.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.Event().wait(), timeout=2.0)
        finally:
            w.close()

    server, host, port = await _start_server(handler)
    try:
        conn = OmniConnection(host=host, port=port, controller_key=CONTROLLER_KEY)
        await conn.connect()
        try:
            received: list[int] = []

            async def consume() -> None:
                async for msg in conn.unsolicited():
                    received.append(msg.opcode)
                    return

            await asyncio.wait_for(consume(), timeout=2.0)
            assert received == [int(OmniLink2MessageType.SystemEvents)]
        finally:
            await conn.close()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_request_timeout() -> None:
    """If the server stays silent, request() raises RequestTimeoutError."""

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        try:
            await _do_full_handshake(r, w)
            # Read whatever the client sends after handshake but never reply.
            with contextlib.suppress(TimeoutError, asyncio.IncompleteReadError):
                await asyncio.wait_for(r.readexactly(4 + 16), timeout=2.0)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.Event().wait(), timeout=2.0)
        finally:
            w.close()

    server, host, port = await _start_server(handler)
    try:
        conn = OmniConnection(host=host, port=port, controller_key=CONTROLLER_KEY)
        await conn.connect()
        try:
            with pytest.raises(RequestTimeoutError):
                await conn.request(
                    OmniLink2MessageType.RequestSystemInformation, timeout=0.2
                )
        finally:
            await conn.close()
    finally:
        server.close()
        await server.wait_closed()
