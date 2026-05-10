"""Mock Omni-Link II controller — async TCP server speaking the panel side.

A drop-in test fixture that lets us exercise the client end of the protocol
without touching real hardware. Reuses the project's own primitives
(``crypto``, ``packet``, ``message``, ``opcodes``) — the wire-level
encryption MUST flow through ``omni_pca.crypto`` to avoid a parallel
implementation drift.

Coverage today:

* Full secure-session handshake (NewSession / SecureSession ack pair)
* ``RequestSystemInformation`` (22) -> ``SystemInformation`` (23)
* ``RequestSystemStatus`` (24)      -> ``SystemStatus`` (25)
* ``RequestProperties`` (32)        -> ``Properties`` (33) for Zone + Unit
* Any other v2 opcode               -> ``Nak`` (2) with the request's opcode
* CRC failures on the inner message -> ``Nak``
* Graceful ``ClientSessionTerminated`` close

References:
    notes/handshake.md (whole document)
    clsOmniLinkConnection.cs:1688-1921 (TCP listener / ack flow)
    clsOL2MsgSystemInformation.cs / clsOL2MsgSystemStatus.cs
    clsOL2MsgRequestProperties.cs / clsOL2MsgProperties.cs
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .crypto import (
    BLOCK_SIZE,
    decrypt_message_payload,
    derive_session_key,
    encrypt_message_payload,
)
from .message import Message, MessageCrcError, MessageFormatError, encode_v2
from .opcodes import OmniLink2MessageType, PacketType
from .packet import Packet

_log = logging.getLogger(__name__)

# enuObjectType (clsOmniLink2.cs / enuObjectType.cs)
_OBJ_ZONE = 1
_OBJ_UNIT = 2
_OBJ_AREA = 5

# Inner-message size constants (model OMNI_PRO_II)
_ZONE_NAME_LEN = 15
_UNIT_NAME_LEN = 12
_AREA_NAME_LEN = 12
_PHONE_LEN = 24

# Wire format for the controller-side ack of NewSession is two literal
# protocol-version bytes followed by the 5-byte SessionID.
_PROTO_HI = 0x00
_PROTO_LO = 0x01

_SESSION_ID_BYTES = 5


@dataclass
class MockState:
    """Programmable panel state. Defaults mimic an Omni Pro II out of the box."""

    model_byte: int = 16  # OMNI_PRO_II
    firmware_major: int = 2
    firmware_minor: int = 12
    firmware_revision: int = 1
    local_phone: str = ""

    # Names by 1-based index (matches Omni's user-facing numbering).
    zones: dict[int, str] = field(default_factory=dict)
    units: dict[int, str] = field(default_factory=dict)
    areas: dict[int, str] = field(default_factory=dict)

    # SystemStatus snapshot. Defaults: time set, battery good, no alarms.
    time_set: bool = True
    year: int = 26  # 2026
    month: int = 5
    day: int = 10
    day_of_week: int = 1  # Sunday=1 in the Omni convention
    hour: int = 12
    minute: int = 0
    second: int = 0
    daylight_saving: int = 0
    sunrise_hour: int = 6
    sunrise_minute: int = 30
    sunset_hour: int = 19
    sunset_minute: int = 45
    battery: int = 200  # 0-255 — typical "good" value

    def zone_name_bytes(self, idx: int) -> bytes:
        return _name_bytes(self.zones.get(idx, ""), _ZONE_NAME_LEN)

    def unit_name_bytes(self, idx: int) -> bytes:
        return _name_bytes(self.units.get(idx, ""), _UNIT_NAME_LEN)

    def area_name_bytes(self, idx: int) -> bytes:
        return _name_bytes(self.areas.get(idx, ""), _AREA_NAME_LEN)


def _name_bytes(name: str, width: int) -> bytes:
    """Encode a panel name as ASCII, right-padded with NULs to a fixed width."""
    raw = name.encode("ascii", errors="replace")[:width]
    return raw + b"\x00" * (width - len(raw))


class MockPanel:
    """Async TCP server that speaks Omni-Link II from the controller side.

    One client at a time — Omni's real controllers are single-session too.
    """

    def __init__(
        self,
        controller_key: bytes,
        state: MockState | None = None,
        session_id_provider: Callable[[], bytes] | None = None,
    ) -> None:
        if len(controller_key) != 16:
            raise ValueError("controller_key must be 16 bytes")
        self._controller_key = bytes(controller_key)
        self.state = state or MockState()
        self._session_id_provider = session_id_provider or (
            lambda: secrets.token_bytes(_SESSION_ID_BYTES)
        )
        self._session_count = 0
        self._last_request_opcode: int | None = None
        self._busy = asyncio.Lock()  # serialise concurrent connection attempts

    # -------- public observables (handy in tests) --------

    @property
    def session_count(self) -> int:
        return self._session_count

    @property
    def last_request_opcode(self) -> int | None:
        return self._last_request_opcode

    # -------- server lifecycle --------

    @asynccontextmanager
    async def serve(
        self, host: str = "127.0.0.1", port: int = 0
    ) -> AsyncIterator[tuple[str, int]]:
        """Start listening; yield ``(host, actual_port)``; tear down on exit."""
        server = await asyncio.start_server(self._handle_client, host=host, port=port)
        sockets = server.sockets or ()
        if not sockets:  # pragma: no cover -- start_server always populates this
            raise RuntimeError("asyncio.start_server returned no sockets")
        bound_host, bound_port = sockets[0].getsockname()[:2]
        _log.debug("mock panel listening on %s:%d", bound_host, bound_port)
        try:
            async with server:
                yield bound_host, bound_port
        finally:
            server.close()
            with contextlib.suppress(Exception):  # pragma: no cover
                await server.wait_closed()

    # -------- connection handling --------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        _log.debug("mock panel: client connected from %s", peer)
        session_key: bytes | None = None
        session_id: bytes | None = None
        try:
            while True:
                header = await _read_exact(reader, 4)
                if header is None:
                    break
                seq = (header[0] << 8) | header[1]
                try:
                    pkt_type = PacketType(header[2])
                except ValueError:
                    _log.debug("mock panel: unknown packet type %#x", header[2])
                    break

                if pkt_type is PacketType.ClientRequestNewSession:
                    session_id, session_key = await self._handle_new_session(seq, writer)

                elif pkt_type is PacketType.ClientRequestSecureSession:
                    if session_key is None or session_id is None:
                        _log.debug("mock panel: secure-session before NewSession")
                        break
                    body = await _read_exact(reader, BLOCK_SIZE)
                    if body is None:
                        break
                    handled = await self._handle_secure_session(
                        seq, body, session_id, session_key, writer
                    )
                    if not handled:
                        break

                elif pkt_type is PacketType.ClientSessionTerminated:
                    _log.debug("mock panel: client requested teardown")
                    break

                elif pkt_type is PacketType.OmniLink2Message:
                    if session_key is None:
                        _log.debug("mock panel: encrypted message before secure session")
                        break
                    cont = await self._handle_encrypted_message(
                        reader, seq, session_key, writer
                    )
                    if not cont:
                        break

                else:
                    _log.debug("mock panel: unhandled packet type %s", pkt_type.name)
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            _log.debug("mock panel: client connection ended unexpectedly")
        finally:
            writer.close()
            with contextlib.suppress(Exception):  # pragma: no cover
                await writer.wait_closed()
            _log.debug("mock panel: client %s disconnected", peer)

    # -------- handshake steps --------

    async def _handle_new_session(
        self, client_seq: int, writer: asyncio.StreamWriter
    ) -> tuple[bytes, bytes]:
        session_id = self._session_id_provider()
        if len(session_id) != _SESSION_ID_BYTES:
            raise RuntimeError(
                f"session_id_provider returned {len(session_id)} bytes,"
                f" need {_SESSION_ID_BYTES}"
            )
        session_key = derive_session_key(self._controller_key, session_id)
        payload = bytes([_PROTO_HI, _PROTO_LO]) + session_id
        ack = Packet(seq=client_seq, type=PacketType.ControllerAckNewSession, data=payload)
        _log.debug("mock panel: ack new session, sid=%s", session_id.hex())
        writer.write(ack.encode())
        await writer.drain()
        return session_id, session_key

    async def _handle_secure_session(
        self,
        client_seq: int,
        ciphertext: bytes,
        session_id: bytes,
        session_key: bytes,
        writer: asyncio.StreamWriter,
    ) -> bool:
        try:
            plaintext = decrypt_message_payload(ciphertext, client_seq, session_key)
        except Exception:
            _log.debug("mock panel: failed to decrypt secure-session request")
            return False
        if not plaintext.startswith(session_id):
            _log.debug(
                "mock panel: secure-session SID mismatch (got %s, want %s)",
                plaintext[:_SESSION_ID_BYTES].hex(),
                session_id.hex(),
            )
            # The real controller replies with ControllerSessionTerminated
            # to signal "your key didn't decrypt right". Mirror that.
            term = Packet(
                seq=client_seq, type=PacketType.ControllerSessionTerminated, data=b""
            )
            writer.write(term.encode())
            await writer.drain()
            return False

        # Echo SessionID back, encrypted with the freshly derived key.
        echo_plain = session_id  # encrypt_message_payload zero-pads for us
        ciphertext_out = encrypt_message_payload(echo_plain, client_seq, session_key)
        ack = Packet(
            seq=client_seq, type=PacketType.ControllerAckSecureSession, data=ciphertext_out
        )
        writer.write(ack.encode())
        await writer.drain()
        self._session_count += 1
        _log.debug("mock panel: secure session up (#%d)", self._session_count)
        return True

    # -------- encrypted message dispatch --------

    async def _handle_encrypted_message(
        self,
        reader: asyncio.StreamReader,
        client_seq: int,
        session_key: bytes,
        writer: asyncio.StreamWriter,
    ) -> bool:
        first_block = await _read_exact(reader, BLOCK_SIZE)
        if first_block is None:
            return False
        first_plain = decrypt_message_payload(first_block, client_seq, session_key)
        # first_plain[0] = StartChar (0x21), first_plain[1] = MessageLength
        msg_length = first_plain[1]
        # Total inner message bytes = msg_length + 4 (start, length, ..., crc1, crc2)
        # We have BLOCK_SIZE bytes; need additional bytes rounded up to BLOCK_SIZE.
        extra_needed = max(0, msg_length + 4 - BLOCK_SIZE)
        rem = (-extra_needed) % BLOCK_SIZE
        extra_aligned = extra_needed + rem
        ciphertext = first_block
        if extra_aligned > 0:
            extra = await _read_exact(reader, extra_aligned)
            if extra is None:
                return False
            ciphertext = first_block + extra
        plaintext = decrypt_message_payload(ciphertext, client_seq, session_key)

        try:
            inner = Message.decode(plaintext)
        except MessageCrcError:
            _log.debug("mock panel: inner message CRC failure")
            await self._send_v2_reply(
                client_seq, _build_nak(0), session_key, writer
            )
            return True
        except MessageFormatError as exc:
            _log.debug("mock panel: malformed inner message: %s", exc)
            return False

        opcode = inner.opcode
        self._last_request_opcode = opcode
        try:
            opcode_name = OmniLink2MessageType(opcode).name
        except ValueError:
            opcode_name = f"Unknown({opcode})"
        _log.debug("mock panel: dispatch opcode=%s payload=%d bytes",
                   opcode_name, len(inner.payload))

        reply = self._dispatch_v2(opcode, inner.payload)
        await self._send_v2_reply(client_seq, reply, session_key, writer)
        return True

    def _dispatch_v2(self, opcode: int, payload: bytes) -> Message:
        if opcode == OmniLink2MessageType.RequestSystemInformation:
            return self._reply_system_information()
        if opcode == OmniLink2MessageType.RequestSystemStatus:
            return self._reply_system_status()
        if opcode == OmniLink2MessageType.RequestProperties:
            return self._reply_properties(payload)
        return _build_nak(opcode)

    # -------- reply builders (byte-exact per clsOL2Msg*.cs) --------

    def _reply_system_information(self) -> Message:
        s = self.state
        revision_byte = s.firmware_revision & 0xFF
        phone = _name_bytes(s.local_phone, _PHONE_LEN)
        body = bytes(
            [
                s.model_byte & 0xFF,
                s.firmware_major & 0xFF,
                s.firmware_minor & 0xFF,
                revision_byte,
            ]
        ) + phone
        return encode_v2(OmniLink2MessageType.SystemInformation, body)

    def _reply_system_status(self) -> Message:
        s = self.state
        body = bytes(
            [
                1 if s.time_set else 0,
                s.year & 0xFF,
                s.month & 0xFF,
                s.day & 0xFF,
                s.day_of_week & 0xFF,
                s.hour & 0xFF,
                s.minute & 0xFF,
                s.second & 0xFF,
                s.daylight_saving & 0xFF,
                s.sunrise_hour & 0xFF,
                s.sunrise_minute & 0xFF,
                s.sunset_hour & 0xFF,
                s.sunset_minute & 0xFF,
                s.battery & 0xFF,
            ]
        )
        # No area alarms appended — real panels can append 2 bytes per area.
        return encode_v2(OmniLink2MessageType.SystemStatus, body)

    def _reply_properties(self, payload: bytes) -> Message:
        # RequestProperties payload (after opcode): ObjectType, IndexNumber(2),
        # RelativeDirection(sbyte), Filter1, Filter2, Filter3.
        if len(payload) < 7:
            return _build_nak(OmniLink2MessageType.RequestProperties)
        obj_type = payload[0]
        index = (payload[1] << 8) | payload[2]
        rel = payload[3]

        store = self._object_store(obj_type)
        if store is None:
            return _build_nak(OmniLink2MessageType.RequestProperties)

        # rel: 0 = exact, 1 = next defined > index, -1/0xFF = previous defined < index.
        if rel == 0:
            target = index if index in store else None
        elif rel == 1:
            candidates = sorted(i for i in store if i > index)
            target = candidates[0] if candidates else None
        elif rel in (0xFF, -1 & 0xFF):  # signed -1 byte
            candidates = sorted((i for i in store if i < index), reverse=True)
            target = candidates[0] if candidates else None
        else:
            return _build_nak(OmniLink2MessageType.RequestProperties)

        if target is None:
            # End of iteration: real panels return EOD (opcode 3) here.
            return encode_v2(OmniLink2MessageType.EOD, b"")

        if obj_type == _OBJ_ZONE:
            return self._build_zone_properties(target)
        if obj_type == _OBJ_UNIT:
            return self._build_unit_properties(target)
        if obj_type == _OBJ_AREA:
            return self._build_area_properties(target)
        return _build_nak(OmniLink2MessageType.RequestProperties)

    def _object_store(self, obj_type: int) -> dict[int, str] | None:
        if obj_type == _OBJ_ZONE:
            return self.state.zones
        if obj_type == _OBJ_UNIT:
            return self.state.units
        if obj_type == _OBJ_AREA:
            return self.state.areas
        return None

    def _build_zone_properties(self, index: int) -> Message:
        # Properties.Data layout for Zone (1-indexed offsets are into Data[]):
        #   [0]=opcode, [1]=ObjectType, [2..3]=ObjectNumber,
        #   [4]=Status, [5]=Loop, [6]=Type, [7]=Area, [8]=Options,
        #   [9..23]=Name (15 bytes)
        # encode_v2 prepends the opcode, so we emit body = Data[1..23].
        body = (
            bytes(
                [
                    _OBJ_ZONE,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    0,  # Status: closed/secure
                    0,  # Loop
                    0,  # Type: EntryExit
                    1,  # Area: default to area 1
                    0,  # Options
                ]
            )
            + self.state.zone_name_bytes(index)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    def _build_unit_properties(self, index: int) -> Message:
        # Properties.Data for Unit:
        #   [0]=opcode, [1]=ObjectType, [2..3]=ObjectNumber,
        #   [4]=UnitStatus, [5..6]=UnitTime, [7]=UnitType,
        #   [8..19]=Name (12), [20]=reserved, [21]=UnitAreas
        body = (
            bytes(
                [
                    _OBJ_UNIT,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    0,  # UnitStatus: off
                    0,
                    0,  # UnitTime
                    1,  # UnitType: Standard
                ]
            )
            + self.state.unit_name_bytes(index)
            + bytes([0, 1])  # reserved + UnitAreas (default area 1)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    def _build_area_properties(self, index: int) -> Message:
        # Properties.Data for Area:
        #   [0]=opcode, [1]=ObjectType, [2..3]=ObjectNumber,
        #   [4]=AreaMode, [5]=AreaAlarms, [6]=EntryTimer, [7]=ExitTimer,
        #   [8]=Enabled, [9]=ExitDelay, [10]=EntryDelay,
        #   [11..22]=Name (12 bytes)
        body = (
            bytes(
                [
                    _OBJ_AREA,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    0,  # AreaMode: Off
                    0,  # AreaAlarms
                    0,  # EntryTimer
                    0,  # ExitTimer
                    1,  # Enabled
                    60,  # ExitDelay (s)
                    30,  # EntryDelay (s)
                ]
            )
            + self.state.area_name_bytes(index)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    # -------- low-level reply send --------

    async def _send_v2_reply(
        self,
        client_seq: int,
        message: Message,
        session_key: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        plaintext = message.encode()
        ciphertext = encrypt_message_payload(plaintext, client_seq, session_key)
        pkt = Packet(seq=client_seq, type=PacketType.OmniLink2Message, data=ciphertext)
        writer.write(pkt.encode())
        await writer.drain()


def _build_nak(in_reply_to_opcode: int) -> Message:
    """Build a v2 Nak. Payload is a single byte echoing the opcode being negged.

    The C# clsOL2MsgNegativeAcknowledge has only the opcode byte; some HAI
    docs show a single trailing data byte but it is not defined. We include
    the offending opcode for ease of debugging — the client side cares only
    that the opcode is Nak.
    """
    return encode_v2(OmniLink2MessageType.Nak, bytes([in_reply_to_opcode & 0xFF]))


async def _read_exact(reader: asyncio.StreamReader, n: int) -> bytes | None:
    """Read exactly ``n`` bytes or return None if EOF arrives early."""
    try:
        return await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        return None
