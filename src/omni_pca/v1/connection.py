"""Async UDP connection to an Omni-Link controller speaking the v1 wire protocol.

Differs from :class:`omni_pca.connection.OmniConnection` in three ways:

1. **Transport**: UDP only. Each datagram carries exactly one outer Packet.
2. **Outer packet type for messages**: ``OmniLinkMessage`` (0x10), not
   ``OmniLink2Message`` (0x20). The 4-step handshake packets are identical.
3. **Inner message format**: v1 ``Message`` with ``StartChar = 0x5A``
   (NonAddressable) carrying a v1 opcode, not the v2 ``StartChar = 0x21``
   carrying a v2 opcode.

The handshake itself (ClientRequestNewSession → ControllerAckNewSession →
ClientRequestSecureSession → ControllerAckSecureSession) and the AES-128
session key derivation are protocol-agnostic and we reuse the same crypto
primitives.

Reference: clsOmniLinkConnection.cs (UDP path):
    udpConnect      lines 1239-1295  open + queue ClientRequestNewSession
    udpListen       lines 1298-1399  receive loop, dispatches replies
    udpHandleRequestNewSession    lines 1401-1459 step 2 → step 3
    udpHandleRequestSecureSession lines 1461-1487 step 4 → OnlineSecure
    udpSend         lines 1514-1560  outer PacketType = OmniLinkMessage (16)
    EncryptPacket   lines 372-401    same crypto as TCP

Cross-references:
    *Two non-public quirks* — Owner's-Manual-style writeup of the
    session-key XOR mix and per-block sequence whitening that this
    handshake relies on: https://hai-omni-pro-ii.warehack.ing/explanation/quirks/
    *Zone & unit numbering* — explains why subsequent ``RequestUnitStatus``
    calls need the long-form (BE u16) payload for unit indices > 255:
    https://hai-omni-pro-ii.warehack.ing/explanation/zone-unit-numbering/
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from enum import IntEnum
from types import TracebackType

from ..crypto import (
    BLOCK_SIZE,
    decrypt_message_payload,
    derive_session_key,
    encrypt_message_payload,
)
from ..message import (
    START_CHAR_V1_UNADDRESSED,
    Message,
    MessageCrcError,
)
from ..opcodes import OmniLinkMessageType, PacketType
from ..packet import Packet

_log = logging.getLogger(__name__)

_DEFAULT_PORT = 4369
_SESSION_ID_LEN = 5
_PROTO_VERSION = (0x00, 0x01)
_MAX_SEQ = 0xFFFF


class ConnectionState(IntEnum):
    DISCONNECTED = 0
    CONNECTING = 1
    NEW_SESSION = 2
    SECURE = 3
    ONLINE = 4


class ConnectionError(OSError):  # noqa: A001 - intentional shadow at module scope
    pass


class HandshakeError(ConnectionError):
    pass


class InvalidEncryptionKeyError(HandshakeError):
    """Controller answered ``ControllerSessionTerminated`` during handshake."""


class ProtocolError(ValueError):
    pass


class RequestTimeoutError(TimeoutError):
    pass


class OmniConnectionV1:
    """UDP + v1-wire-format connection to an Omni-Link controller."""

    def __init__(
        self,
        host: str,
        port: int = _DEFAULT_PORT,
        controller_key: bytes = b"",
        timeout: float = 5.0,
        retry_count: int = 3,
    ) -> None:
        if len(controller_key) != 16:
            raise ValueError(
                f"controller_key must be 16 bytes, got {len(controller_key)}"
            )
        self._host = host
        self._port = port
        self._controller_key = bytes(controller_key)
        self._default_timeout = timeout
        self._retry_count = max(0, retry_count)

        self._udp_transport: asyncio.DatagramTransport | None = None
        self._udp_protocol: _OmniDatagramProtocol | None = None

        self._state = ConnectionState.DISCONNECTED
        self._session_id: bytes | None = None
        self._session_key: bytes | None = None

        # First wire packet uses seq=1; wraparound skips 0 (reserved for
        # unsolicited inbound). See clsOmniLinkConnection.cs:1251 (UDP
        # init pktSequence=1, then udpSend pre-increments).
        self._next_seq: int = 1

        self._pending: dict[int, asyncio.Future[Packet]] = {}
        self._unsolicited_queue: asyncio.Queue[Message] = asyncio.Queue()

        self._handshake_event: asyncio.Event = asyncio.Event()
        self._handshake_packet: Packet | None = None
        self._handshake_error: Exception | None = None

        self._closed = False

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def session_key(self) -> bytes | None:
        return self._session_key

    async def __aenter__(self) -> OmniConnectionV1:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._state is not ConnectionState.DISCONNECTED:
            raise ConnectionError(
                f"already connecting/connected (state={self._state})"
            )
        self._state = ConnectionState.CONNECTING
        try:
            loop = asyncio.get_running_loop()
            self._udp_transport, self._udp_protocol = (
                await loop.create_datagram_endpoint(
                    lambda: _OmniDatagramProtocol(self),
                    remote_addr=(self._host, self._port),
                )
            )
        except (TimeoutError, OSError) as exc:
            self._state = ConnectionState.DISCONNECTED
            raise ConnectionError(f"failed to open UDP socket: {exc}") from exc

        try:
            await self._do_handshake()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Tear down. Politely terminate the panel session first.

        Without ClientSessionTerminated the panel keeps our slot allocated
        until its idle timeout — and rejects subsequent connect attempts
        with ControllerCannotStartNewSession (0x07).
        """
        if self._closed:
            return
        self._closed = True
        previous_state = self._state
        self._state = ConnectionState.DISCONNECTED

        if previous_state in (
            ConnectionState.NEW_SESSION,
            ConnectionState.SECURE,
            ConnectionState.ONLINE,
        ):
            try:
                term = Packet(
                    seq=self._claim_seq(),
                    type=PacketType.ClientSessionTerminated,
                    data=b"",
                )
                self._write_packet(term)
            except Exception as exc:  # noqa: BLE001 - close() must be idempotent
                _log.debug("close: failed to send ClientSessionTerminated: %s", exc)

        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("connection closed"))
        self._pending.clear()

        if self._udp_transport is not None:
            with contextlib.suppress(OSError):
                self._udp_transport.close()
            self._udp_transport = None
            self._udp_protocol = None

    # ---- public request API ---------------------------------------------

    async def request(
        self,
        opcode: OmniLinkMessageType | int,
        payload: bytes = b"",
        timeout: float | None = None,
    ) -> Message:
        """Send a v1 request, await the matching reply, return the inner Message."""
        if self._state is not ConnectionState.ONLINE:
            raise ConnectionError(
                f"cannot send request, connection state={self._state.name}"
            )
        message = Message(
            start_char=START_CHAR_V1_UNADDRESSED,
            data=bytes([int(opcode)]) + payload,
        )
        per_attempt_timeout = timeout if timeout is not None else self._default_timeout
        max_attempts = 1 + self._retry_count
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            seq, fut = self._send_encrypted(message)
            try:
                reply_packet = await asyncio.wait_for(fut, per_attempt_timeout)
            except TimeoutError as exc:
                last_exc = exc
                self._pending.pop(seq, None)
                if attempt < max_attempts:
                    _log.debug(
                        "udp v1 retry %d/%d on opcode=%d seq=%d",
                        attempt, max_attempts, int(opcode), seq,
                    )
                    continue
                raise RequestTimeoutError(
                    f"no v1 reply for opcode={int(opcode)} "
                    f"after {max_attempts} attempt(s)"
                ) from last_exc
            return self._decode_inner(reply_packet)
        raise RequestTimeoutError(
            f"request loop exited without reply for opcode={int(opcode)}"
        )

    async def iter_streaming(
        self,
        initial_op: OmniLinkMessageType | int,
        *,
        ack_op: OmniLinkMessageType | int = OmniLinkMessageType.Ack,
        end_op: OmniLinkMessageType | int = OmniLinkMessageType.EOD,
        nak_op: OmniLinkMessageType | int = OmniLinkMessageType.Nak,
        timeout: float | None = None,
    ) -> AsyncIterator[Message]:
        """Drive a v1 lock-step streaming download (UploadNames / UploadSetup / etc).

        Sends ``initial_op`` (no payload), yields each ``ack_op``-elicited
        reply, and stops when the panel sends ``end_op``. ``nak_op`` is
        treated as an immediate end-of-stream — no exception (some
        firmwares use NAK to signal "no records to upload").

        Unlike :meth:`request` we don't retry on timeout — losing a
        reply mid-stream desynchronises the conversation, so the right
        answer is to surface the timeout and let the caller restart.
        """
        if self._state is not ConnectionState.ONLINE:
            raise ConnectionError(
                f"cannot stream, connection state={self._state.name}"
            )
        per_reply_timeout = timeout if timeout is not None else self._default_timeout

        # Step 1: send the initial bare-opcode request, wait for first reply.
        first_msg = Message(
            start_char=START_CHAR_V1_UNADDRESSED,
            data=bytes([int(initial_op)]),
        )
        seq, fut = self._send_encrypted(first_msg)
        try:
            reply_pkt = await asyncio.wait_for(fut, per_reply_timeout)
        except TimeoutError as exc:
            self._pending.pop(seq, None)
            raise RequestTimeoutError(
                f"no first reply to streaming opcode={int(initial_op)}"
            ) from exc
        reply = self._decode_inner(reply_pkt)

        # Step 2..N: ack-and-receive until end_op or nak_op.
        while True:
            if reply.opcode == int(end_op) or reply.opcode == int(nak_op):
                return
            yield reply

            ack_msg = Message(
                start_char=START_CHAR_V1_UNADDRESSED,
                data=bytes([int(ack_op)]),
            )
            seq, fut = self._send_encrypted(ack_msg)
            try:
                reply_pkt = await asyncio.wait_for(fut, per_reply_timeout)
            except TimeoutError as exc:
                self._pending.pop(seq, None)
                raise RequestTimeoutError(
                    f"no reply after streaming Ack (seq={seq})"
                ) from exc
            reply = self._decode_inner(reply_pkt)

    def unsolicited(self) -> AsyncIterator[Message]:
        queue = self._unsolicited_queue

        async def _gen() -> AsyncIterator[Message]:
            while True:
                yield await queue.get()

        return _gen()

    # ---- handshake -------------------------------------------------------

    async def _do_handshake(self) -> None:
        # Step 1: empty ClientRequestNewSession.
        self._state = ConnectionState.NEW_SESSION
        step1 = Packet(
            seq=self._claim_seq(),
            type=PacketType.ClientRequestNewSession,
            data=b"",
        )
        self._write_packet(step1)

        # Step 2: ControllerAckNewSession (carries protocol version + SessionID).
        ack1 = await self._await_handshake_packet()
        if ack1.type is PacketType.ControllerCannotStartNewSession:
            raise HandshakeError("controller cannot start new session (busy?)")
        if ack1.type is not PacketType.ControllerAckNewSession:
            raise HandshakeError(f"unexpected step-2 packet type {ack1.type.name}")
        if len(ack1.data) < 7:
            raise HandshakeError(
                f"ControllerAckNewSession payload too short: {len(ack1.data)} bytes"
            )
        if (ack1.data[0], ack1.data[1]) != _PROTO_VERSION:
            raise HandshakeError(
                f"unsupported protocol version {ack1.data[0]:#04x}{ack1.data[1]:02x}"
            )
        self._session_id = bytes(ack1.data[2 : 2 + _SESSION_ID_LEN])
        self._session_key = derive_session_key(self._controller_key, self._session_id)

        # Step 3: encrypted ClientRequestSecureSession echoing SessionID.
        self._state = ConnectionState.SECURE
        step3_seq = self._claim_seq()
        step3_ct = encrypt_message_payload(
            self._session_id, step3_seq, self._session_key
        )
        step3 = Packet(
            seq=step3_seq,
            type=PacketType.ClientRequestSecureSession,
            data=step3_ct,
        )
        self._write_packet(step3)

        # Step 4: ControllerAckSecureSession (or termination).
        ack2 = await self._await_handshake_packet()
        if ack2.type is PacketType.ControllerSessionTerminated:
            raise InvalidEncryptionKeyError(
                "controller terminated session during handshake (wrong ControllerKey?)"
            )
        if ack2.type is not PacketType.ControllerAckSecureSession:
            raise HandshakeError(
                f"unexpected step-4 packet type {ack2.type.name}"
            )
        self._state = ConnectionState.ONLINE

    async def _await_handshake_packet(self) -> Packet:
        try:
            await asyncio.wait_for(
                self._handshake_event.wait(), self._default_timeout
            )
        except TimeoutError as exc:
            raise HandshakeError(
                "timeout waiting for controller handshake reply"
            ) from exc
        if self._handshake_error is not None:
            err = self._handshake_error
            self._handshake_error = None
            raise err
        pkt = self._handshake_packet
        self._handshake_packet = None
        self._handshake_event.clear()
        if pkt is None:
            raise HandshakeError("handshake event fired with no packet")
        return pkt

    # ---- send / receive helpers -----------------------------------------

    def _claim_seq(self) -> int:
        seq = self._next_seq
        nxt = seq + 1
        if nxt > _MAX_SEQ or nxt == 0:
            nxt = 1
        self._next_seq = nxt
        return seq

    def _send_encrypted(
        self, inner: Message
    ) -> tuple[int, asyncio.Future[Packet]]:
        if self._session_key is None:
            raise ConnectionError("no session key (handshake not complete)")
        seq = self._claim_seq()
        plaintext = inner.encode()
        ciphertext = encrypt_message_payload(plaintext, seq, self._session_key)
        # KEY DIFFERENCE FROM V2: outer type is OmniLinkMessage (0x10),
        # not OmniLink2Message (0x20). See clsOmniLinkConnection.cs:1536.
        pkt = Packet(seq=seq, type=PacketType.OmniLinkMessage, data=ciphertext)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Packet] = loop.create_future()
        self._pending[seq] = fut
        self._write_packet(pkt)
        return seq, fut

    def _write_packet(self, pkt: Packet) -> None:
        if self._udp_transport is None:
            raise ConnectionError("transport not open")
        wire = pkt.encode()
        _log.debug(
            "TX seq=%d type=%s len=%d", pkt.seq, pkt.type.name, len(pkt.data)
        )
        self._udp_transport.sendto(wire)

    def _decode_inner(self, pkt: Packet) -> Message:
        if self._session_key is None:
            raise ConnectionError("no session key")
        if not pkt.data:
            raise ProtocolError("empty packet data")
        plaintext = decrypt_message_payload(pkt.data, pkt.seq, self._session_key)
        try:
            return Message.decode(plaintext)
        except MessageCrcError as exc:
            raise ProtocolError(f"inner v1 message CRC mismatch: {exc}") from exc

    # ---- inbound dispatch (called from the datagram protocol) -----------

    def _dispatch(self, pkt: Packet) -> None:
        if pkt.data is None:
            pkt = Packet(seq=pkt.seq, type=pkt.type, data=b"")

        if self._state in (ConnectionState.NEW_SESSION, ConnectionState.SECURE):
            handshake_types = {
                PacketType.ControllerAckNewSession,
                PacketType.ControllerAckSecureSession,
                PacketType.ControllerSessionTerminated,
                PacketType.ControllerCannotStartNewSession,
            }
            if pkt.type in handshake_types:
                self._handshake_packet = pkt
                self._handshake_event.set()
                return

        if pkt.seq == 0:
            if pkt.type is PacketType.OmniLinkMessage:
                try:
                    msg = self._decode_inner(pkt)
                except (ProtocolError, ConnectionError) as exc:
                    _log.warning(
                        "dropping malformed unsolicited v1 packet: %s", exc
                    )
                    return
                try:
                    self._unsolicited_queue.put_nowait(msg)
                except asyncio.QueueFull:  # pragma: no cover - unbounded queue
                    _log.warning("unsolicited queue full; dropping message")
            return

        fut = self._pending.pop(pkt.seq, None)
        if fut is None:
            _log.debug(
                "no waiter for seq=%d type=%s; dropping",
                pkt.seq, pkt.type.name,
            )
            return
        if pkt.type is PacketType.ControllerSessionTerminated:
            fut.set_exception(ConnectionError("controller terminated session"))
            return
        if not fut.done():
            fut.set_result(pkt)


class _OmniDatagramProtocol(asyncio.DatagramProtocol):
    """asyncio.DatagramProtocol bound to a single OmniConnectionV1.

    Each datagram is one complete Packet. We decode it and hand it to the
    connection's dispatcher; the dispatcher already knows how to sort
    handshake / solicited / unsolicited paths.
    """

    def __init__(self, conn: OmniConnectionV1) -> None:
        self._conn = conn

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        pass

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            pkt = Packet.decode(data)
        except Exception as exc:
            _log.warning("dropping malformed UDP datagram: %s", exc)
            return
        try:
            self._conn._dispatch(pkt)
        except Exception:
            _log.exception("UDP v1 dispatch crashed for seq=%d", pkt.seq)

    def error_received(self, exc: Exception) -> None:
        _log.warning("UDP v1 socket error: %s", exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None:
            _log.warning("UDP v1 transport lost: %s", exc)
