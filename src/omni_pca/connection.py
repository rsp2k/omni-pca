"""Async TCP connection to an HAI/Leviton Omni-Link II controller.

This is the foundation layer. It owns the asyncio TCP socket, drives the
4-step secure-session handshake, frames inner ``Message`` objects inside
outer ``Packet`` envelopes, and routes solicited replies (matched by the
client's outer sequence number) to per-request ``Future`` objects while
shoveling unsolicited push packets (seq=0) into a queue exposed via
:meth:`OmniConnection.unsolicited`.

References (line numbers into HAI/pca-re/decompiled/project/HAI_Shared
/clsOmniLinkConnection.cs):
    1688-1697  send empty ClientRequestNewSession on connect
    1714-1758  TCP frame reader: per-block decrypt-to-learn-length pattern
    1796       solicited reply matched by SequenceNumber == pktSequence
    1847-1854  unsolicited dispatch when SequenceNumber == 0
    1864-1921  step 2 handler: derive key, enqueue step 3
    1923-1947  step 4 handler: transition to OnlineSecure
    1808       ControllerSessionTerminated during handshake => InvalidEncryptionKey
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from enum import IntEnum
from types import TracebackType

from .crypto import (
    BLOCK_SIZE,
    decrypt_message_payload,
    derive_session_key,
    encrypt_message_payload,
)
from .message import Message, MessageCrcError, encode_v2
from .opcodes import OmniLink2MessageType, PacketType
from .packet import MIN_PACKET_BYTES, Packet

_log = logging.getLogger(__name__)

_DEFAULT_PORT = 4369
_HEADER_BYTES = MIN_PACKET_BYTES  # 4
_SESSION_ID_LEN = 5
_PROTO_VERSION = (0x00, 0x01)
_MAX_SEQ = 0xFFFF


class ConnectionState(IntEnum):
    """High-level state of the secure session."""

    DISCONNECTED = 0
    CONNECTING = 1
    NEW_SESSION = 2  # ClientRequestNewSession sent, awaiting ControllerAckNewSession
    SECURE = 3  # ClientRequestSecureSession sent, awaiting ControllerAckSecureSession
    ONLINE = 4


# ---- exceptions ----------------------------------------------------------


class ConnectionError(OSError):  # noqa: A001 - intentional shadow at module scope
    """Generic transport-level failure (TCP closed unexpectedly, etc.)."""


class HandshakeError(ConnectionError):
    """The 4-step secure-session handshake did not complete."""


class InvalidEncryptionKeyError(HandshakeError):
    """Controller answered ``ControllerSessionTerminated`` during handshake.

    Per clsOmniLinkConnection.cs:1808, this is the panel's way of saying
    "your derived SessionKey didn't decrypt my echo correctly" — i.e. the
    ControllerKey we used doesn't match the panel's NVRAM.
    """


class ProtocolError(ValueError):
    """A received frame was structurally invalid."""


class RequestTimeoutError(TimeoutError):
    """A solicited request did not receive a reply in time."""


# ---- the connection ------------------------------------------------------


class OmniConnection:
    """Low-level async Omni-Link II connection.

    Use as an async context manager:

    .. code-block:: python

        async with OmniConnection(host, port, controller_key) as conn:
            reply = await conn.request(OmniLink2MessageType.RequestSystemInformation)
    """

    def __init__(
        self,
        host: str,
        port: int = _DEFAULT_PORT,
        controller_key: bytes = b"",
        timeout: float = 5.0,
    ) -> None:
        if len(controller_key) != 16:
            raise ValueError(
                f"controller_key must be 16 bytes, got {len(controller_key)}"
            )
        self._host = host
        self._port = port
        self._controller_key = bytes(controller_key)
        self._default_timeout = timeout

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._state = ConnectionState.DISCONNECTED

        self._session_id: bytes | None = None
        self._session_key: bytes | None = None

        # Client-side outbound sequence counter. The very first wire packet
        # uses seq=1; every subsequent client packet bumps by 1, skipping 0
        # on wraparound (0 is reserved for unsolicited inbound).
        self._next_seq: int = 1

        # Solicited replies are matched on the seq number they were sent
        # with; the controller echoes that seq back on the reply.
        self._pending: dict[int, asyncio.Future[Packet]] = {}

        # Unsolicited inbound messages (panel-pushed events) land here.
        self._unsolicited_queue: asyncio.Queue[Message] = asyncio.Queue()

        # Hands the handshake's step 2/4 packets to connect() while the
        # reader task is running. Step 2 carries the SessionID; step 4 is
        # just the encrypted ack.
        self._handshake_event: asyncio.Event = asyncio.Event()
        self._handshake_packet: Packet | None = None
        self._handshake_error: Exception | None = None

        self._reader_task: asyncio.Task[None] | None = None
        self._closed = False

    # ---- lifecycle -------------------------------------------------------

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def session_key(self) -> bytes | None:
        """The derived per-session AES key, or ``None`` before handshake."""
        return self._session_key

    async def __aenter__(self) -> OmniConnection:
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
        """Open the TCP socket and run the 4-step secure-session handshake."""
        if self._state is not ConnectionState.DISCONNECTED:
            raise ConnectionError(f"already connecting/connected (state={self._state})")
        self._state = ConnectionState.CONNECTING
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=self._default_timeout,
            )
        except (TimeoutError, OSError) as exc:
            self._state = ConnectionState.DISCONNECTED
            raise ConnectionError(f"failed to open TCP socket: {exc}") from exc

        self._reader_task = asyncio.create_task(
            self._read_loop(), name=f"omni-conn-reader-{self._host}"
        )

        try:
            await self._do_handshake()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        """Tear down the TCP socket and reader task. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._state = ConnectionState.DISCONNECTED

        # Cancel anyone still waiting for a reply.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("connection closed"))
        self._pending.clear()

        if self._writer is not None:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except (OSError, RuntimeError):
                pass
            self._writer = None
        self._reader = None

        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader_task
        self._reader_task = None

    # ---- public request / receive API -----------------------------------

    async def request(
        self,
        opcode: OmniLink2MessageType | int,
        payload: bytes = b"",
        timeout: float | None = None,
    ) -> Message:
        """Send a v2 request, await the matching reply, return the inner Message.

        The reply is matched on the outer packet sequence number (the
        controller echoes the client's seq for solicited replies). On
        timeout the pending future is removed and ``RequestTimeoutError``
        is raised.
        """
        if self._state is not ConnectionState.ONLINE:
            raise ConnectionError(
                f"cannot send request, connection state={self._state.name}"
            )
        message = encode_v2(opcode, payload)
        seq, fut = self._send_encrypted(message)
        try:
            reply_packet = await asyncio.wait_for(
                fut, timeout if timeout is not None else self._default_timeout
            )
        except TimeoutError as exc:
            self._pending.pop(seq, None)
            raise RequestTimeoutError(
                f"no reply for opcode={int(opcode)} seq={seq}"
            ) from exc
        return self._decode_inner(reply_packet)

    def unsolicited(self) -> AsyncIterator[Message]:
        """Async iterator over unsolicited inbound messages (seq=0)."""
        queue = self._unsolicited_queue

        async def _gen() -> AsyncIterator[Message]:
            while True:
                msg = await queue.get()
                yield msg

        return _gen()

    # ---- handshake -------------------------------------------------------

    async def _do_handshake(self) -> None:
        # Step 1: send empty ClientRequestNewSession (cleartext, seq=1).
        self._state = ConnectionState.NEW_SESSION
        step1 = Packet(
            seq=self._claim_seq(),
            type=PacketType.ClientRequestNewSession,
            data=b"",
        )
        self._write_packet(step1)

        # Step 2: wait for ControllerAckNewSession.
        ack1 = await self._await_handshake_packet()
        if ack1.type is PacketType.ControllerCannotStartNewSession:
            raise HandshakeError("controller cannot start new session (busy?)")
        if ack1.type is not PacketType.ControllerAckNewSession:
            raise HandshakeError(
                f"unexpected step-2 packet type {ack1.type.name}"
            )
        if len(ack1.data) < 7:
            raise HandshakeError(
                f"ControllerAckNewSession payload too short: {len(ack1.data)} bytes"
            )
        proto_hi, proto_lo = ack1.data[0], ack1.data[1]
        if (proto_hi, proto_lo) != _PROTO_VERSION:
            raise HandshakeError(
                f"unsupported protocol version {proto_hi:#04x}{proto_lo:02x}, "
                f"want {_PROTO_VERSION[0]:#04x}{_PROTO_VERSION[1]:02x}"
            )
        self._session_id = bytes(ack1.data[2 : 2 + _SESSION_ID_LEN])
        self._session_key = derive_session_key(self._controller_key, self._session_id)

        # Step 3: send ClientRequestSecureSession with the SessionID echoed
        # back, AES-encrypted under the freshly derived SessionKey. The
        # crypto layer handles zero-padding to a 16-byte block + the
        # per-block sequence-number whitening.
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
        self._write_packet(step3, encrypted=True)

        # Step 4: wait for ControllerAckSecureSession (or termination).
        ack2 = await self._await_handshake_packet()
        if ack2.type is PacketType.ControllerSessionTerminated:
            raise InvalidEncryptionKeyError(
                "controller terminated session during handshake "
                "(wrong ControllerKey?)"
            )
        if ack2.type is not PacketType.ControllerAckSecureSession:
            raise HandshakeError(
                f"unexpected step-4 packet type {ack2.type.name}"
            )
        # We don't bother validating the decrypted plaintext — per
        # clsOmniLinkConnection.cs:1933-1937, neither does PC Access.
        # If AES decrypted without throwing, we trust the key matched.
        self._state = ConnectionState.ONLINE

    async def _await_handshake_packet(self) -> Packet:
        try:
            await asyncio.wait_for(
                self._handshake_event.wait(), self._default_timeout
            )
        except TimeoutError as exc:
            raise HandshakeError("timeout waiting for controller handshake reply") from exc
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
        """Allocate the next client-side outbound sequence number.

        Wraparound: after 0xFFFF we go to 1, skipping 0 because seq=0 is
        reserved for unsolicited inbound packets and would collide with
        the dispatch logic.
        """
        seq = self._next_seq
        nxt = seq + 1
        if nxt > _MAX_SEQ:
            nxt = 1
        if nxt == 0:  # paranoia; shouldn't happen with above branch
            nxt = 1
        self._next_seq = nxt
        return seq

    def _send_encrypted(
        self, inner: Message
    ) -> tuple[int, asyncio.Future[Packet]]:
        """Frame an inner v2 ``Message`` as an encrypted ``OmniLink2Message`` packet."""
        if self._session_key is None:
            raise ConnectionError("no session key (handshake not complete)")
        seq = self._claim_seq()
        plaintext = inner.encode()
        ciphertext = encrypt_message_payload(plaintext, seq, self._session_key)
        pkt = Packet(seq=seq, type=PacketType.OmniLink2Message, data=ciphertext)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Packet] = loop.create_future()
        self._pending[seq] = fut
        self._write_packet(pkt, encrypted=True)
        return seq, fut

    def _write_packet(self, pkt: Packet, *, encrypted: bool = False) -> None:
        if self._writer is None:
            raise ConnectionError("transport not open")
        wire = pkt.encode()
        _log.debug(
            "TX seq=%d type=%s len=%d encrypted=%s",
            pkt.seq,
            pkt.type.name,
            len(pkt.data),
            encrypted,
        )
        self._writer.write(wire)

    def _decode_inner(self, pkt: Packet) -> Message:
        """Decrypt + parse the inner ``Message`` from an OmniLink2Message packet."""
        if self._session_key is None:
            raise ConnectionError("no session key")
        if not pkt.data:
            raise ProtocolError("empty packet data")
        plaintext = decrypt_message_payload(pkt.data, pkt.seq, self._session_key)
        try:
            return Message.decode(plaintext)
        except MessageCrcError as exc:
            raise ProtocolError(f"inner message CRC mismatch: {exc}") from exc

    # ---- reader loop -----------------------------------------------------

    async def _read_loop(self) -> None:
        """Drain the TCP socket forever, dispatching each frame.

        Frame logic mirrors clsOmniLinkConnection.cs:1714-1758:
            * Read 4-byte header (seq, type, reserved=0).
            * For OmniLink2Message: read ONE 16-byte block, decrypt, peek
              at the inner ``length`` byte to learn how many more 16-byte
              blocks remain, then read those.
            * For control packets (ack-new-session, etc.): read the
              type-specific fixed payload size.
        """
        try:
            assert self._reader is not None
            reader = self._reader
            while not self._closed:
                header = await self._read_exact(reader, _HEADER_BYTES)
                if header is None:
                    break
                if header[3] != 0:
                    raise ProtocolError(
                        f"reserved byte non-zero: {header[3]:#04x}"
                    )
                seq = (header[0] << 8) | header[1]
                try:
                    type_byte = PacketType(header[2])
                except ValueError as exc:
                    raise ProtocolError(
                        f"unknown packet type {header[2]:#04x}"
                    ) from exc

                payload = await self._read_payload(reader, seq, type_byte)
                if payload is None:
                    break
                pkt = Packet(seq=seq, type=type_byte, data=payload)
                _log.debug(
                    "RX seq=%d type=%s len=%d", pkt.seq, pkt.type.name, len(pkt.data)
                )
                self._dispatch(pkt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.warning("reader loop crashed: %s", exc, exc_info=True)
            # Wake up handshake waiters with the error so connect() unwinds.
            if self._state in (ConnectionState.NEW_SESSION, ConnectionState.SECURE):
                self._handshake_error = exc
                self._handshake_event.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
            self._pending.clear()

    async def _read_payload(
        self, reader: asyncio.StreamReader, seq: int, type_byte: PacketType
    ) -> bytes | None:
        """Read the payload bytes for one packet, given its already-parsed header.

        Returns ``None`` if the socket closed mid-packet.
        """
        if type_byte is PacketType.ControllerAckNewSession:
            return await self._read_exact(reader, 7)
        if type_byte is PacketType.ControllerAckSecureSession:
            return await self._read_exact(reader, BLOCK_SIZE)
        if type_byte is PacketType.OmniLink2Message:
            return await self._read_encrypted_message(reader, seq)
        if type_byte is PacketType.OmniLink2UnencryptedMessage:
            return await self._read_unencrypted_message(reader)
        if type_byte in (
            PacketType.ControllerSessionTerminated,
            PacketType.ControllerCannotStartNewSession,
            PacketType.ClientSessionTerminated,
            PacketType.NoMessage,
        ):
            return b""
        raise ProtocolError(
            f"unhandled inbound packet type {type_byte.name}"
        )

    async def _read_encrypted_message(
        self, reader: asyncio.StreamReader, seq: int
    ) -> bytes | None:
        """Read N 16-byte blocks for an OmniLink2Message frame.

        We have to decrypt the FIRST block to learn the inner ``length``
        byte, then compute how many more 16-byte blocks the rest of the
        message occupies. The reference C# code does this same dance
        (clsOmniLinkConnection.cs:1731-1758).
        """
        first = await self._read_exact(reader, BLOCK_SIZE)
        if first is None:
            return None
        if self._session_key is None:
            # Could happen if we get an encrypted frame before handshake;
            # bail out the hard way.
            raise ProtocolError("encrypted frame before session key derived")
        first_plain = decrypt_message_payload(first, seq, self._session_key)
        # first_plain[0] is the StartChar (0x21 for v2), [1] is MessageLength.
        message_length = first_plain[1]
        # Bytes already consumed inside the first block (after StartChar
        # and length): the inner frame is [start][length][data...][crc lo/hi]
        # so total inner size is message_length + 4. We have 16 bytes of
        # ciphertext == 16 bytes of plaintext, of which the inner frame
        # could be shorter (rest is zero pad). Need to read the rest, in
        # whole 16-byte blocks.
        remaining_inner = message_length + 4 - BLOCK_SIZE
        if remaining_inner <= 0:
            extra_bytes = 0
        else:
            pad = (-remaining_inner) % BLOCK_SIZE
            extra_bytes = remaining_inner + pad
        if extra_bytes == 0:
            return first
        rest = await self._read_exact(reader, extra_bytes)
        if rest is None:
            return None
        return first + rest

    async def _read_unencrypted_message(
        self, reader: asyncio.StreamReader
    ) -> bytes | None:
        """Read an OmniLink2UnencryptedMessage frame.

        Cleartext mirrors of the encrypted path; layout is just the inner
        ``Message`` bytes one-to-one. We read 5 bytes (start + len + 1
        opcode byte minimum + 2 CRC), then any remaining payload.
        """
        head = await self._read_exact(reader, 5)
        if head is None:
            return None
        # head = [start][length][opcode][crc_lo][crc_hi] for length=1.
        length = head[1]
        if length <= 1:
            return head
        rest = await self._read_exact(reader, length - 1)
        if rest is None:
            return None
        return head + rest

    async def _read_exact(
        self, reader: asyncio.StreamReader, n: int
    ) -> bytes | None:
        try:
            data = await reader.readexactly(n)
        except asyncio.IncompleteReadError:
            return None
        return data

    def _dispatch(self, pkt: Packet) -> None:
        """Route an inbound packet to its waiter (handshake / request / unsolicited)."""
        # During the handshake, control packets carrying the session
        # information go to the handshake awaiter regardless of seq.
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

        # Unsolicited push from the panel — seq=0.
        if pkt.seq == 0:
            if pkt.type is PacketType.OmniLink2Message:
                try:
                    msg = self._decode_inner(pkt)
                except (ProtocolError, ConnectionError) as exc:
                    _log.warning("dropping malformed unsolicited packet: %s", exc)
                    return
                try:
                    self._unsolicited_queue.put_nowait(msg)
                except asyncio.QueueFull:  # pragma: no cover - unbounded queue
                    _log.warning("unsolicited queue full; dropping message")
            return

        # Solicited reply — match on the seq we sent.
        fut = self._pending.pop(pkt.seq, None)
        if fut is None:
            _log.debug(
                "no waiter for seq=%d type=%s; dropping", pkt.seq, pkt.type.name
            )
            return
        if pkt.type is PacketType.ControllerSessionTerminated:
            fut.set_exception(
                ConnectionError("controller terminated session")
            )
            return
        if not fut.done():
            fut.set_result(pkt)
