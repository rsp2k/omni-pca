"""Unit tests for omni_pca.mock_panel.

These tests drive the mock with raw primitives only (Packet / Message /
crypto.*) so they double as a sanity check on the handshake and on the
inner-message encoding. Do NOT import the in-progress OmniClient here —
the point is to keep the mock testable independently.
"""

from __future__ import annotations

import asyncio

import pytest

from omni_pca.crypto import (
    BLOCK_SIZE,
    decrypt_message_payload,
    derive_session_key,
    encrypt_message_payload,
)
from omni_pca.message import Message, crc16_modbus, encode_v2
from omni_pca.mock_panel import MockPanel, MockState
from omni_pca.opcodes import OmniLink2MessageType, PacketType
from omni_pca.packet import Packet

CONTROLLER_KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
KNOWN_SID = bytes.fromhex("0102030405")


async def _readexact(reader: asyncio.StreamReader, n: int) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), timeout=2.0)


async def _do_handshake(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, session_id: bytes
) -> bytes:
    """Run NewSession + SecureSession; return the derived session key."""
    # Step 1: client sends ClientRequestNewSession (seq=2, no payload).
    writer.write(Packet(seq=2, type=PacketType.ClientRequestNewSession).encode())
    await writer.drain()

    # Step 2: read 4-byte header + 7-byte payload.
    header = await _readexact(reader, 4)
    assert header[2] == int(PacketType.ControllerAckNewSession)
    payload = await _readexact(reader, 7)
    assert payload[0] == 0x00
    assert payload[1] == 0x01
    assert payload[2:7] == session_id

    session_key = derive_session_key(CONTROLLER_KEY, session_id)

    # Step 3: encrypt the SessionID and send ClientRequestSecureSession (seq=3).
    ciphertext = encrypt_message_payload(session_id, 3, session_key)
    writer.write(
        Packet(
            seq=3, type=PacketType.ClientRequestSecureSession, data=ciphertext
        ).encode()
    )
    await writer.drain()

    # Step 4: read 4-byte header + 16-byte payload, decrypt, verify echo.
    header = await _readexact(reader, 4)
    assert header[2] == int(PacketType.ControllerAckSecureSession)
    body = await _readexact(reader, BLOCK_SIZE)
    plain = decrypt_message_payload(body, 3, session_key)
    assert plain[: len(session_id)] == session_id
    return session_key


async def _send_v2(
    writer: asyncio.StreamWriter,
    seq: int,
    opcode: OmniLink2MessageType | int,
    payload: bytes,
    session_key: bytes,
) -> None:
    msg = encode_v2(opcode, payload)
    ciphertext = encrypt_message_payload(msg.encode(), seq, session_key)
    writer.write(
        Packet(seq=seq, type=PacketType.OmniLink2Message, data=ciphertext).encode()
    )
    await writer.drain()


async def _recv_v2(
    reader: asyncio.StreamReader, seq: int, session_key: bytes
) -> Message:
    """Read one v2 reply using the same two-step framing as the real client."""
    header = await _readexact(reader, 4)
    assert header[2] == int(PacketType.OmniLink2Message)
    first = await _readexact(reader, BLOCK_SIZE)
    first_plain = decrypt_message_payload(first, seq, session_key)
    msg_length = first_plain[1]
    extra_needed = max(0, msg_length + 4 - BLOCK_SIZE)
    rem = (-extra_needed) % BLOCK_SIZE
    extra_aligned = extra_needed + rem
    if extra_aligned:
        extra = await _readexact(reader, extra_aligned)
        ciphertext = first + extra
    else:
        ciphertext = first
    plain = decrypt_message_payload(ciphertext, seq, session_key)
    return Message.decode(plain)


@pytest.fixture
def known_sid_panel() -> MockPanel:
    return MockPanel(
        controller_key=CONTROLLER_KEY,
        session_id_provider=lambda: KNOWN_SID,
    )


async def test_handshake_completes_with_known_session_id(known_sid_panel: MockPanel) -> None:
    async with known_sid_panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            session_key = await _do_handshake(reader, writer, KNOWN_SID)
            assert session_key == derive_session_key(CONTROLLER_KEY, KNOWN_SID)
            assert known_sid_panel.session_count == 1
        finally:
            writer.close()
            await writer.wait_closed()


async def test_request_system_information_returns_model_byte() -> None:
    state = MockState(
        model_byte=16, firmware_major=2, firmware_minor=12, firmware_revision=1
    )
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=state,
        session_id_provider=lambda: KNOWN_SID,
    )
    async with panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            session_key = await _do_handshake(reader, writer, KNOWN_SID)
            await _send_v2(
                writer, 4, OmniLink2MessageType.RequestSystemInformation, b"", session_key
            )
            reply = await _recv_v2(reader, 4, session_key)
            assert reply.opcode == int(OmniLink2MessageType.SystemInformation)
            assert reply.payload[0] == 16  # model byte
            assert reply.payload[1] == 2  # major
            assert reply.payload[2] == 12  # minor
            assert reply.payload[3] == 1  # revision
            assert panel.last_request_opcode == int(
                OmniLink2MessageType.RequestSystemInformation
            )
        finally:
            writer.close()
            await writer.wait_closed()


async def test_request_properties_for_a_zone() -> None:
    state = MockState(zones={1: "FRONT DOOR"})
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=state,
        session_id_provider=lambda: KNOWN_SID,
    )
    async with panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            session_key = await _do_handshake(reader, writer, KNOWN_SID)
            # ObjectType=Zone(1), IndexNumber=1, RelativeDirection=0, three filter zeros.
            req_payload = bytes([1, 0x00, 0x01, 0, 0, 0, 0])
            await _send_v2(
                writer, 4, OmniLink2MessageType.RequestProperties, req_payload, session_key
            )
            reply = await _recv_v2(reader, 4, session_key)
            assert reply.opcode == int(OmniLink2MessageType.Properties)
            data = reply.payload  # everything after the opcode
            assert data[0] == 1  # ObjectType=Zone
            assert (data[1] << 8) | data[2] == 1  # ObjectNumber
            name_bytes = data[8:23]
            assert name_bytes.rstrip(b"\x00").decode("ascii") == "FRONT DOOR"
        finally:
            writer.close()
            await writer.wait_closed()


async def test_unknown_opcode_returns_nak() -> None:
    panel = MockPanel(
        controller_key=CONTROLLER_KEY, session_id_provider=lambda: KNOWN_SID
    )
    async with panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            session_key = await _do_handshake(reader, writer, KNOWN_SID)
            # Pick something obviously unimplemented in the mock.
            await _send_v2(
                writer, 4, OmniLink2MessageType.RequestEventLogItem, b"\x00\x00\x00",
                session_key,
            )
            reply = await _recv_v2(reader, 4, session_key)
            assert reply.opcode == int(OmniLink2MessageType.Nak)
        finally:
            writer.close()
            await writer.wait_closed()


async def test_bad_crc_returns_nak_or_disconnect() -> None:
    panel = MockPanel(
        controller_key=CONTROLLER_KEY, session_id_provider=lambda: KNOWN_SID
    )
    async with panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            session_key = await _do_handshake(reader, writer, KNOWN_SID)
            # Build a v2 message manually with a corrupted CRC.
            opcode = int(OmniLink2MessageType.RequestSystemInformation)
            length = 1
            body = bytes([0x21, length, opcode])
            good_crc = crc16_modbus(bytes([length, opcode]))
            bad_crc = good_crc ^ 0xFFFF
            wire = body + bytes([bad_crc & 0xFF, (bad_crc >> 8) & 0xFF])
            ciphertext = encrypt_message_payload(wire, 4, session_key)
            writer.write(
                Packet(seq=4, type=PacketType.OmniLink2Message, data=ciphertext).encode()
            )
            await writer.drain()
            # Either we get a Nak back or the panel hangs up. Both are acceptable.
            try:
                reply = await _recv_v2(reader, 4, session_key)
            except (asyncio.IncompleteReadError, ConnectionError):
                return
            assert reply.opcode == int(OmniLink2MessageType.Nak)
        finally:
            writer.close()
            await writer.wait_closed()


async def test_unencrypted_request_new_session_does_not_require_encryption() -> None:
    # The first packet of the handshake MUST work with no crypto in scope.
    panel = MockPanel(
        controller_key=CONTROLLER_KEY, session_id_provider=lambda: KNOWN_SID
    )
    async with panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            writer.write(
                Packet(seq=2, type=PacketType.ClientRequestNewSession).encode()
            )
            await writer.drain()
            header = await _readexact(reader, 4)
            assert header[2] == int(PacketType.ControllerAckNewSession)
            payload = await _readexact(reader, 7)
            assert payload[:2] == b"\x00\x01"
            assert payload[2:] == KNOWN_SID
        finally:
            writer.close()
            await writer.wait_closed()


async def test_request_properties_for_a_unit() -> None:
    state = MockState(units={2: "PORCH LIGHT"})
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=state,
        session_id_provider=lambda: KNOWN_SID,
    )
    async with panel.serve() as (host, port):
        reader, writer = await asyncio.open_connection(host, port)
        try:
            session_key = await _do_handshake(reader, writer, KNOWN_SID)
            # ObjectType=Unit(2), IndexNumber=2.
            req_payload = bytes([2, 0x00, 0x02, 0, 0, 0, 0])
            await _send_v2(
                writer, 4, OmniLink2MessageType.RequestProperties, req_payload, session_key
            )
            reply = await _recv_v2(reader, 4, session_key)
            data = reply.payload
            assert data[0] == 2  # Unit
            assert (data[1] << 8) | data[2] == 2  # ObjectNumber
            # Per clsOL2MsgProperties.cs: Unit name is at Data[8..19], i.e. payload[7..18].
            unit_name = data[7:19].rstrip(b"\x00").decode("ascii")
            assert unit_name == "PORCH LIGHT"
        finally:
            writer.close()
            await writer.wait_closed()
