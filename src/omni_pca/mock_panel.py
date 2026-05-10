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
* ``RequestProperties`` (32)        -> ``Properties`` (33) for Zone + Unit + Area
* ``Command`` (20)                  -> ``Ack`` (1) / ``Nak`` (2), with state mutation
* ``ExecuteSecurityCommand`` (74)   -> ``Ack`` (1) (or Nak on bad code), with state
* ``RequestStatus`` (34)            -> ``Status`` (35) for Zone/Unit/Area/Thermostat
* ``RequestExtendedStatus`` (58)    -> ``ExtendedStatus`` (59) for the same set
* ``AcknowledgeAlerts`` (60)        -> ``Ack`` (1)
* Synthesized push of ``SystemEvents`` (55, seq=0) when state mutates
* Any other v2 opcode               -> ``Nak`` (2) with the request's opcode
* CRC failures on the inner message -> ``Nak``
* Graceful ``ClientSessionTerminated`` close

References:
    notes/handshake.md (whole document)
    clsOmniLinkConnection.cs:1688-1921 (TCP listener / ack flow)
    clsOL2MsgSystemInformation.cs / clsOL2MsgSystemStatus.cs
    clsOL2MsgRequestProperties.cs / clsOL2MsgProperties.cs
    clsOL2MsgCommand.cs / clsOL2MsgExecuteSecurityCommand.cs
    clsOL2MsgRequestStatus.cs / clsOL2MsgStatus.cs
    clsOL2MsgRequestExtendedStatus.cs / clsOL2MsgExtendedStatus.cs
    clsOLMsgSystemEvents.cs
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from .commands import Command
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
_OBJ_BUTTON = 3
_OBJ_AREA = 5
_OBJ_THERMOSTAT = 6

# Inner-message size constants (model OMNI_PRO_II)
_ZONE_NAME_LEN = 15
_UNIT_NAME_LEN = 12
_AREA_NAME_LEN = 12
_BUTTON_NAME_LEN = 12
_THERMOSTAT_NAME_LEN = 12
_PHONE_LEN = 24

# Per-object-type record sizes for the basic Status (opcode 35) reply.
# Source: clsOL2MsgStatus.cs:13-27 — sizes hard-coded per object type, no
# per-record length byte.
_STATUS_RECORD_SIZES: dict[int, int] = {
    _OBJ_ZONE: 4,        # number(2) + status + loop
    _OBJ_UNIT: 5,        # number(2) + state + time(2)
    _OBJ_AREA: 6,        # number(2) + mode + alarms + entry + exit
    _OBJ_THERMOSTAT: 9,  # number(2) + status + 6 bytes
}

# Per-object-type ExtendedStatus (opcode 59) record sizes. The reply carries
# this byte at payload[1] (object_length); we use these to build the reply.
# Source: clsOL2MsgExtendedStatus.cs (per-object body offsets).
_EXTENDED_STATUS_RECORD_SIZES: dict[int, int] = {
    _OBJ_ZONE: 4,         # number(2) + status + loop
    _OBJ_UNIT: 5,         # number(2) + state + time(2) — ZigBeePower optional
    _OBJ_AREA: 6,         # number(2) + mode + alarms + entry + exit
    _OBJ_THERMOSTAT: 14,  # number(2) + status + temp + heat + cool + sys + fan + hold + humidity + h_set + dh_set + outdoor + horc
}

# Wire format for the controller-side ack of NewSession is two literal
# protocol-version bytes followed by the 5-byte SessionID.
_PROTO_HI = 0x00
_PROTO_LO = 0x01

_SESSION_ID_BYTES = 5

# Small delay before pushing a synthesized SystemEvents so the request future
# resolves first. Kept tiny; tests use asyncio.wait_for with their own timeout.
_PUSH_DELAY = 0.005


# --------------------------------------------------------------------------
# Per-object state dataclasses
# --------------------------------------------------------------------------


@dataclass
class MockUnitState:
    """One programmable unit (light / output / scene)."""

    name: str = ""
    state: int = 0  # 0=off, 1=on, 100..200=brightness percent (raw Omni)
    time_remaining: int = 0


@dataclass
class MockAreaState:
    """One programmable security area."""

    name: str = ""
    mode: int = 0  # SecurityMode value (Off=0, Day=1, Night=2, Away=3, ...)
    last_user: int = 0
    entry_timer: int = 0
    exit_timer: int = 0
    alarms: int = 0


@dataclass
class MockZoneState:
    """One programmable security zone."""

    name: str = ""
    current_state: int = 0  # 0=secure, 1=not-ready, 2=trouble, 3=tamper
    latched_state: int = 0  # 0=secure, 4=tripped, 8=reset (raw bits 2-3)
    arming_state: int = 0   # 0=disarmed, 16=armed, 32=bypassed, 48=auto-bypassed
    is_bypassed: bool = False
    loop: int = 0           # analog loop reading

    @property
    def status_byte(self) -> int:
        """Compose the on-the-wire status byte from the sub-fields.

        Encoding mirrors clsZone.cs:385 / clsText.cs:3110:
            bits 0-1 → current_state (0..3)
            bits 2-3 → latched_state (0/4/8)
            bits 4-5 → arming_state  (0/16/32/48)
        is_bypassed forces the arming bits to BYPASSED (0x20) regardless of
        the underlying arming_state value.
        """
        val = (self.current_state & 0x03) | (self.latched_state & 0x0C)
        if self.is_bypassed:
            val |= 0x20
        else:
            val |= self.arming_state & 0x30
        return val & 0xFF


@dataclass
class MockButtonState:
    """One programmable button macro (no live state — buttons just fire programs)."""

    name: str = ""


@dataclass
class MockThermostatState:
    """One programmable thermostat. Defaults are sane Omni Pro II values."""

    name: str = ""
    temperature_raw: int = 168     # ~76°F on Omni linear scale
    heat_setpoint_raw: int = 144   # ~62°F
    cool_setpoint_raw: int = 184   # ~80°F
    system_mode: int = 0           # HvacMode: 0=Off, 1=Heat, 2=Cool, 3=Auto, 4=EmHeat
    fan_mode: int = 0              # FanMode:  0=Auto, 1=On, 2=Cycle
    hold_mode: int = 0             # HoldMode: 0=Off, 1=Hold, 2=Vacation
    humidity_raw: int = 0
    humidify_setpoint_raw: int = 0
    dehumidify_setpoint_raw: int = 0
    outdoor_temperature_raw: int = 0
    horc_status: int = 0
    status: int = 1                # 1 = communicating with the panel


@dataclass
class MockState:
    """Programmable panel state. Defaults mimic an Omni Pro II out of the box.

    Backward compat: callers may pass ``zones={1: "FRONT DOOR"}`` (a plain
    ``dict[int, str]``) and the constructor will auto-promote the strings
    into the appropriate ``Mock*State`` instance.
    """

    model_byte: int = 16  # OMNI_PRO_II
    firmware_major: int = 2
    firmware_minor: int = 12
    firmware_revision: int = 1
    local_phone: str = ""

    # Per-object state machines, by 1-based index. Values may be passed as
    # plain strings (interpreted as the object's name) or as the matching
    # ``Mock*State`` dataclass instance.
    zones: dict[int, MockZoneState] = field(default_factory=dict)
    units: dict[int, MockUnitState] = field(default_factory=dict)
    areas: dict[int, MockAreaState] = field(default_factory=dict)
    thermostats: dict[int, MockThermostatState] = field(default_factory=dict)
    buttons: dict[int, MockButtonState] = field(default_factory=dict)

    # User-code table for ExecuteSecurityCommand validation.
    # Mapping is ``{code_index: 4-digit pin}``; the panel returns the
    # matched code_index in the area's last_user field on success.
    user_codes: dict[int, int] = field(default_factory=dict)

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

    def __post_init__(self) -> None:
        """Promote bare-string values into per-type state dataclasses.

        This keeps the existing ``MockState(zones={1: "FRONT DOOR"})``
        call sites working unchanged, while letting new code pass full
        ``MockZoneState`` / ``MockUnitState`` / etc. records.
        """
        self.zones = _promote_dict(self.zones, MockZoneState)
        self.units = _promote_dict(self.units, MockUnitState)
        self.areas = _promote_dict(self.areas, MockAreaState)
        self.thermostats = _promote_dict(self.thermostats, MockThermostatState)
        self.buttons = _promote_dict(self.buttons, MockButtonState)

    # ---- name-bytes helpers (kept for back-compat with old callers) -----

    def zone_name_bytes(self, idx: int) -> bytes:
        z = self.zones.get(idx)
        return _name_bytes(z.name if z else "", _ZONE_NAME_LEN)

    def unit_name_bytes(self, idx: int) -> bytes:
        u = self.units.get(idx)
        return _name_bytes(u.name if u else "", _UNIT_NAME_LEN)

    def area_name_bytes(self, idx: int) -> bytes:
        a = self.areas.get(idx)
        return _name_bytes(a.name if a else "", _AREA_NAME_LEN)

    def thermostat_name_bytes(self, idx: int) -> bytes:
        t = self.thermostats.get(idx)
        return _name_bytes(t.name if t else "", _THERMOSTAT_NAME_LEN)

    def button_name_bytes(self, idx: int) -> bytes:
        b = self.buttons.get(idx)
        return _name_bytes(b.name if b else "", _BUTTON_NAME_LEN)


def _promote_dict(
    raw: dict[int, object],
    dataclass_cls: type,
) -> dict[int, object]:
    """Walk a ``{int: str | DataclassInstance}`` dict, wrapping bare strings.

    Bare strings become ``dataclass_cls(name=string)``. Anything that is
    already an instance of ``dataclass_cls`` (or anything else) passes
    through untouched.
    """
    out: dict[int, object] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            out[k] = dataclass_cls(name=v)
        else:
            out[k] = v
    return out


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
        # Per-connection state captured on _handle_client; used by the
        # synthesized-event push helper when state mutates.
        self._active_writer: asyncio.StreamWriter | None = None
        self._active_session_key: bytes | None = None
        self._push_tasks: set[asyncio.Task[None]] = set()

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
            # Cancel any in-flight push tasks so the test event loop
            # tears down cleanly.
            for t in list(self._push_tasks):
                if not t.done():
                    t.cancel()
            self._push_tasks.clear()
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
                    # Make session info available to push helpers.
                    self._active_writer = writer
                    self._active_session_key = session_key

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
            self._active_writer = None
            self._active_session_key = None
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

        reply, push_words = self._dispatch_v2(opcode, inner.payload)
        await self._send_v2_reply(client_seq, reply, session_key, writer)
        if push_words:
            self._schedule_event_push(push_words, session_key, writer)
        return True

    def _dispatch_v2(
        self, opcode: int, payload: bytes
    ) -> tuple[Message, tuple[int, ...]]:
        """Dispatch a single decoded request and return (reply, push_event_words).

        ``push_event_words`` is a (possibly empty) tuple of 16-bit event
        words to push as an unsolicited SystemEvents (opcode 55) frame
        AFTER the synchronous reply has been written.
        """
        if opcode == OmniLink2MessageType.RequestSystemInformation:
            return self._reply_system_information(), ()
        if opcode == OmniLink2MessageType.RequestSystemStatus:
            return self._reply_system_status(), ()
        if opcode == OmniLink2MessageType.RequestProperties:
            return self._reply_properties(payload), ()
        if opcode == OmniLink2MessageType.Command:
            return self._handle_command(payload)
        if opcode == OmniLink2MessageType.ExecuteSecurityCommand:
            return self._handle_execute_security_command(payload)
        if opcode == OmniLink2MessageType.RequestStatus:
            return self._reply_status(payload), ()
        if opcode == OmniLink2MessageType.RequestExtendedStatus:
            return self._reply_extended_status(payload), ()
        if opcode == OmniLink2MessageType.AcknowledgeAlerts:
            return _build_ack(), ()
        return _build_nak(opcode), ()

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
        if obj_type == _OBJ_THERMOSTAT:
            return self._build_thermostat_properties(target)
        if obj_type == _OBJ_BUTTON:
            return self._build_button_properties(target)
        return _build_nak(OmniLink2MessageType.RequestProperties)

    def _object_store(self, obj_type: int) -> dict[int, object] | None:
        if obj_type == _OBJ_ZONE:
            return self.state.zones  # type: ignore[return-value]
        if obj_type == _OBJ_UNIT:
            return self.state.units  # type: ignore[return-value]
        if obj_type == _OBJ_AREA:
            return self.state.areas  # type: ignore[return-value]
        if obj_type == _OBJ_THERMOSTAT:
            return self.state.thermostats  # type: ignore[return-value]
        if obj_type == _OBJ_BUTTON:
            return self.state.buttons  # type: ignore[return-value]
        return None

    def _build_zone_properties(self, index: int) -> Message:
        # Properties.Data layout for Zone (1-indexed offsets are into Data[]):
        #   [0]=opcode, [1]=ObjectType, [2..3]=ObjectNumber,
        #   [4]=Status, [5]=Loop, [6]=Type, [7]=Area, [8]=Options,
        #   [9..23]=Name (15 bytes)
        # encode_v2 prepends the opcode, so we emit body = Data[1..23].
        zone = self.state.zones.get(index)
        body = (
            bytes(
                [
                    _OBJ_ZONE,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    zone.status_byte if zone else 0,
                    zone.loop if zone else 0,
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
        unit = self.state.units.get(index)
        body = (
            bytes(
                [
                    _OBJ_UNIT,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    unit.state if unit else 0,
                    (unit.time_remaining >> 8) & 0xFF if unit else 0,
                    unit.time_remaining & 0xFF if unit else 0,
                    1,  # UnitType: Standard
                ]
            )
            + self.state.unit_name_bytes(index)
            + bytes([0, 1])  # reserved + UnitAreas (default area 1)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    def _build_thermostat_properties(self, index: int) -> Message:
        # Properties.Data layout for Thermostat (Data[0]=opcode, body starts
        # at Data[1]. ``payload`` here strips the opcode; payload[i]==Data[i+1]):
        #   payload[0]    object type (Thermostat = 6)
        #   payload[1..2] object number (BE u16)
        #   payload[3]    communicating flag
        #   payload[4]    temperature raw
        #   payload[5]    heat setpoint raw
        #   payload[6]    cool setpoint raw
        #   payload[7]    mode
        #   payload[8]    fan mode
        #   payload[9]    hold mode
        #   payload[10]   thermostat type
        #   payload[11..22] 12-byte name
        t = self.state.thermostats.get(index)
        body = (
            bytes(
                [
                    _OBJ_THERMOSTAT,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    t.status if t else 0,                    # communicating flag
                    t.temperature_raw if t else 0,
                    t.heat_setpoint_raw if t else 0,
                    t.cool_setpoint_raw if t else 0,
                    t.system_mode if t else 0,
                    t.fan_mode if t else 0,
                    t.hold_mode if t else 0,
                    1,                                       # thermostat type: AUTO_HEAT_COOL
                ]
            )
            + self.state.thermostat_name_bytes(index)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    def _build_button_properties(self, index: int) -> Message:
        # Properties.Data layout for Button:
        #   payload[0]      object type (Button = 3)
        #   payload[1..2]   object number (BE u16)
        #   payload[3..14]  12-byte name (NUL-padded)
        body = (
            bytes(
                [
                    _OBJ_BUTTON,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                ]
            )
            + self.state.button_name_bytes(index)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    def _build_area_properties(self, index: int) -> Message:
        # Properties.Data for Area:
        #   [0]=opcode, [1]=ObjectType, [2..3]=ObjectNumber,
        #   [4]=AreaMode, [5]=AreaAlarms, [6]=EntryTimer, [7]=ExitTimer,
        #   [8]=Enabled, [9]=ExitDelay, [10]=EntryDelay,
        #   [11..22]=Name (12 bytes)
        area = self.state.areas.get(index)
        body = (
            bytes(
                [
                    _OBJ_AREA,
                    (index >> 8) & 0xFF,
                    index & 0xFF,
                    area.mode if area else 0,
                    area.alarms if area else 0,
                    area.entry_timer if area else 0,
                    area.exit_timer if area else 0,
                    1,  # Enabled
                    60,  # ExitDelay (s)
                    30,  # EntryDelay (s)
                ]
            )
            + self.state.area_name_bytes(index)
        )
        return encode_v2(OmniLink2MessageType.Properties, body)

    # -------- Status (opcode 34/35) and ExtendedStatus (opcode 58/59) --------

    def _reply_status(self, payload: bytes) -> Message:
        """Build a Status (opcode 35) reply for a RequestStatus (opcode 34).

        RequestStatus payload (5 bytes, clsOL2MsgRequestStatus.cs):
            [0]    object type
            [1..2] starting number (BE u16)
            [3..4] ending number   (BE u16)

        Status reply payload layout (clsOL2MsgStatus.cs):
            [0]    object type
            [1..]  N records of size :data:`_STATUS_RECORD_SIZES[object_type]`
        """
        if len(payload) < 5:
            return _build_nak(OmniLink2MessageType.RequestStatus)
        obj_type = payload[0]
        start = (payload[1] << 8) | payload[2]
        end = (payload[3] << 8) | payload[4]
        store = self._object_store(obj_type)
        if store is None or obj_type not in _STATUS_RECORD_SIZES:
            return _build_nak(OmniLink2MessageType.RequestStatus)
        body = bytearray([obj_type])
        for idx in range(start, end + 1):
            obj = store.get(idx)
            if obj is None:
                continue
            body.extend(_status_record(obj_type, idx, obj))
        if len(body) == 1:
            # No matching objects in range — return EOD per protocol.
            return encode_v2(OmniLink2MessageType.EOD, b"")
        return encode_v2(OmniLink2MessageType.Status, bytes(body))

    def _reply_extended_status(self, payload: bytes) -> Message:
        """Build an ExtendedStatus (opcode 59) reply for opcode 58.

        ExtendedStatus reply payload layout (clsOL2MsgExtendedStatus.cs):
            [0]    object type
            [1]    object length (per-record byte count)
            [2..]  N records of ``object_length`` bytes
        """
        if len(payload) < 5:
            return _build_nak(OmniLink2MessageType.RequestExtendedStatus)
        obj_type = payload[0]
        start = (payload[1] << 8) | payload[2]
        end = (payload[3] << 8) | payload[4]
        store = self._object_store(obj_type)
        record_size = _EXTENDED_STATUS_RECORD_SIZES.get(obj_type, 0)
        if store is None or record_size == 0:
            return _build_nak(OmniLink2MessageType.RequestExtendedStatus)
        body = bytearray([obj_type, record_size])
        any_records = False
        for idx in range(start, end + 1):
            obj = store.get(idx)
            if obj is None:
                continue
            body.extend(_extended_status_record(obj_type, idx, obj))
            any_records = True
        if not any_records:
            return encode_v2(OmniLink2MessageType.EOD, b"")
        return encode_v2(OmniLink2MessageType.ExtendedStatus, bytes(body))

    # -------- Command (opcode 20) --------

    def _handle_command(self, payload: bytes) -> tuple[Message, tuple[int, ...]]:
        """Apply a Command (opcode 20) and return (reply, push_event_words).

        Command payload (4 bytes, clsOL2MsgCommand.cs after stripping opcode):
            [0] command byte (enuUnitCommand)
            [1] parameter1   (single byte; brightness, mode, code index, ...)
            [2] parameter2 high byte (BE u16)
            [3] parameter2 low  byte (object number for nearly every command)
        """
        if len(payload) < 4:
            return _build_nak(OmniLink2MessageType.Command), ()
        cmd_byte = payload[0]
        param1 = payload[1]
        param2 = (payload[2] << 8) | payload[3]
        try:
            cmd = Command(cmd_byte)
        except ValueError:
            _log.debug("mock panel: unknown command byte %d", cmd_byte)
            return _build_nak(OmniLink2MessageType.Command), ()

        push: tuple[int, ...] = ()

        if cmd == Command.UNIT_OFF:
            unit = self._ensure_unit(param2)
            unit.state = 0
            unit.time_remaining = 0
            push = (_unit_state_changed_word(param2, 0),)
        elif cmd == Command.UNIT_ON:
            unit = self._ensure_unit(param2)
            unit.state = 1
            unit.time_remaining = 0
            push = (_unit_state_changed_word(param2, 1),)
        elif cmd == Command.UNIT_LEVEL:
            # Per enuUnitCommand.Level (line 15): param1 = 0..100 percent.
            # Encoded into the state byte as 100..200.
            if not 0 <= param1 <= 100:
                return _build_nak(OmniLink2MessageType.Command), ()
            unit = self._ensure_unit(param2)
            unit.state = 100 + param1
            unit.time_remaining = 0
            push = (_unit_state_changed_word(param2, 1 if param1 > 0 else 0),)
        elif cmd == Command.BYPASS_ZONE:
            zone = self._ensure_zone(param2)
            zone.is_bypassed = True
            push = (_zone_state_changed_word(param2, 1),)
        elif cmd == Command.RESTORE_ZONE:
            zone = self._ensure_zone(param2)
            zone.is_bypassed = False
            push = (_zone_state_changed_word(param2, 0),)
        elif cmd == Command.SET_THERMOSTAT_HEAT_SETPOINT:
            tstat = self._ensure_thermostat(param2)
            tstat.heat_setpoint_raw = param1
        elif cmd == Command.SET_THERMOSTAT_COOL_SETPOINT:
            tstat = self._ensure_thermostat(param2)
            tstat.cool_setpoint_raw = param1
        elif cmd == Command.SET_THERMOSTAT_SYSTEM_MODE:
            tstat = self._ensure_thermostat(param2)
            tstat.system_mode = param1
        elif cmd == Command.SET_THERMOSTAT_FAN_MODE:
            tstat = self._ensure_thermostat(param2)
            tstat.fan_mode = param1
        elif cmd == Command.SET_THERMOSTAT_HOLD_MODE:
            tstat = self._ensure_thermostat(param2)
            tstat.hold_mode = param1
        else:
            # Acknowledge but don't model: EXECUTE_BUTTON, EXECUTE_PROGRAM,
            # SHOW_MESSAGE_*, CLEAR_MESSAGE, scenes, audio, energy, ...
            _log.info(
                "mock panel: command %s (byte=%d, p1=%d, p2=%d) acknowledged "
                "with no state effect",
                cmd.name, cmd_byte, param1, param2,
            )

        return _build_ack(), push

    # -------- ExecuteSecurityCommand (opcode 74) --------

    def _handle_execute_security_command(
        self, payload: bytes
    ) -> tuple[Message, tuple[int, ...]]:
        """Validate the user code, mutate area state, push an ArmingChanged event.

        Payload (6 bytes, clsOL2MsgExecuteSecurityCommand.cs after stripping opcode):
            [0] area number (1-based)
            [1] security mode (raw enuSecurityMode 0..7)
            [2..5] code digits (thousands, hundreds, tens, ones)

        Implementation choice: on success we return a plain Ack (opcode 1)
        rather than ExecuteSecurityCommandResponse (opcode 75) — the Omni
        firmware varies and the client treats both as success. On bad-code
        we return Nak (the simplest panel behaviour); the client raises
        :class:`CommandFailedError` either way.
        """
        if len(payload) < 6:
            return _build_nak(OmniLink2MessageType.ExecuteSecurityCommand), ()
        area_idx = payload[0]
        mode = payload[1]
        code = (
            payload[2] * 1000 + payload[3] * 100 + payload[4] * 10 + payload[5]
        )

        # Find a matching code in user_codes. The matched code_index is
        # what the panel records as the "last user" for the area.
        matched_user = None
        for user_idx, pin in self.state.user_codes.items():
            if pin == code:
                matched_user = user_idx
                break
        if matched_user is None:
            _log.debug("mock panel: ExecuteSecurityCommand bad code %04d", code)
            return _build_nak(OmniLink2MessageType.ExecuteSecurityCommand), ()

        area = self._ensure_area(area_idx)
        area.mode = mode
        area.last_user = matched_user

        push = (_arming_changed_word(area_idx, mode, matched_user),)
        return _build_ack(), push

    # -------- per-object ensure helpers --------

    def _ensure_unit(self, idx: int) -> MockUnitState:
        unit = self.state.units.get(idx)
        if unit is None:
            unit = MockUnitState()
            self.state.units[idx] = unit
        return unit

    def _ensure_zone(self, idx: int) -> MockZoneState:
        zone = self.state.zones.get(idx)
        if zone is None:
            zone = MockZoneState()
            self.state.zones[idx] = zone
        return zone

    def _ensure_area(self, idx: int) -> MockAreaState:
        area = self.state.areas.get(idx)
        if area is None:
            area = MockAreaState()
            self.state.areas[idx] = area
        return area

    def _ensure_thermostat(self, idx: int) -> MockThermostatState:
        tstat = self.state.thermostats.get(idx)
        if tstat is None:
            tstat = MockThermostatState()
            self.state.thermostats[idx] = tstat
        return tstat

    # -------- low-level reply send + push helpers --------

    def _schedule_event_push(
        self,
        event_words: tuple[int, ...],
        session_key: bytes,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Fire-and-forget: push a SystemEvents (opcode 55) frame after a tiny delay.

        The delay lets the synchronous reply hit the client first so the
        request future resolves before the unsolicited event arrives. Tests
        that wait on ``client.events()`` use ``asyncio.wait_for`` with their
        own timeout to fail fast if the push never arrives.
        """

        async def _push() -> None:
            try:
                await asyncio.sleep(_PUSH_DELAY)
                msg = _build_system_events_message(event_words)
                # Push goes out with seq=0 so the client routes it to the
                # unsolicited queue (clsOmniLinkConnection.cs:1847-1854).
                await self._send_v2_reply(0, msg, session_key, writer)
            except (ConnectionError, asyncio.CancelledError):
                pass
            except Exception:  # pragma: no cover - diagnostic only
                _log.exception("mock panel: failed to push synthesized event")

        task = asyncio.create_task(_push(), name="mock-panel-event-push")
        self._push_tasks.add(task)
        task.add_done_callback(self._push_tasks.discard)

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


# --------------------------------------------------------------------------
# Status / ExtendedStatus per-record builders
# --------------------------------------------------------------------------


def _status_record(obj_type: int, idx: int, obj: object) -> bytes:
    """Build one record of a basic Status (opcode 35) reply for ``obj_type``."""
    if obj_type == _OBJ_ZONE:
        z = obj  # type: ignore[assignment]
        assert isinstance(z, MockZoneState)
        return bytes([(idx >> 8) & 0xFF, idx & 0xFF, z.status_byte, z.loop])
    if obj_type == _OBJ_UNIT:
        u = obj  # type: ignore[assignment]
        assert isinstance(u, MockUnitState)
        return bytes(
            [
                (idx >> 8) & 0xFF,
                idx & 0xFF,
                u.state & 0xFF,
                (u.time_remaining >> 8) & 0xFF,
                u.time_remaining & 0xFF,
            ]
        )
    if obj_type == _OBJ_AREA:
        a = obj  # type: ignore[assignment]
        assert isinstance(a, MockAreaState)
        return bytes(
            [
                (idx >> 8) & 0xFF,
                idx & 0xFF,
                a.mode & 0xFF,
                a.alarms & 0xFF,
                a.entry_timer & 0xFF,
                a.exit_timer & 0xFF,
            ]
        )
    if obj_type == _OBJ_THERMOSTAT:
        t = obj  # type: ignore[assignment]
        assert isinstance(t, MockThermostatState)
        return bytes(
            [
                (idx >> 8) & 0xFF,
                idx & 0xFF,
                t.status & 0xFF,
                t.temperature_raw & 0xFF,
                t.heat_setpoint_raw & 0xFF,
                t.cool_setpoint_raw & 0xFF,
                t.system_mode & 0xFF,
                t.fan_mode & 0xFF,
                t.hold_mode & 0xFF,
            ]
        )
    raise AssertionError(f"unhandled object type {obj_type}")


def _extended_status_record(obj_type: int, idx: int, obj: object) -> bytes:
    """Build one record of an ExtendedStatus (opcode 59) reply for ``obj_type``.

    The basic-status records are byte-compatible with the extended-status
    records for Zone, Unit, and Area (the ExtendedStatus reply just adds
    the per-record length byte at payload[1]). Thermostat is the only type
    where the extended record is wider — it adds humidity/outdoor/horc
    fields at the end.
    """
    if obj_type in (_OBJ_ZONE, _OBJ_UNIT, _OBJ_AREA):
        return _status_record(obj_type, idx, obj)
    if obj_type == _OBJ_THERMOSTAT:
        t = obj  # type: ignore[assignment]
        assert isinstance(t, MockThermostatState)
        return bytes(
            [
                (idx >> 8) & 0xFF,
                idx & 0xFF,
                t.status & 0xFF,
                t.temperature_raw & 0xFF,
                t.heat_setpoint_raw & 0xFF,
                t.cool_setpoint_raw & 0xFF,
                t.system_mode & 0xFF,
                t.fan_mode & 0xFF,
                t.hold_mode & 0xFF,
                t.humidity_raw & 0xFF,
                t.humidify_setpoint_raw & 0xFF,
                t.dehumidify_setpoint_raw & 0xFF,
                t.outdoor_temperature_raw & 0xFF,
                t.horc_status & 0xFF,
            ]
        )
    raise AssertionError(f"unhandled object type {obj_type}")


# --------------------------------------------------------------------------
# SystemEvents (opcode 55) — synthesized push frames
# --------------------------------------------------------------------------


def _build_system_events_message(words: tuple[int, ...]) -> Message:
    """Pack one or more 16-bit event words into a v2 SystemEvents Message.

    Each word is encoded big-endian. Reference: clsOLMsgSystemEvents.cs.
    """
    body = bytearray()
    for w in words:
        body.append((w >> 8) & 0xFF)
        body.append(w & 0xFF)
    return encode_v2(OmniLink2MessageType.SystemEvents, bytes(body))


def _zone_state_changed_word(zone_index: int, new_state: int) -> int:
    """Encode a ZONE_STATE_CHANGE (top 6 bits == 0x4) event word.

    Layout (matches events._classify):
        bits 10-15: family marker (0x0400)
        bit  9    : new_state (0=secure, 1=open)
        low byte  : zone index 1..255
    """
    word = 0x0400 | (zone_index & 0xFF)
    if new_state:
        word |= 0x0200
    return word & 0xFFFF


def _unit_state_changed_word(unit_index: int, new_state: int) -> int:
    """Encode a UNIT_STATE_CHANGE (top 6 bits == 0x8) event word.

    Layout:
        bits 10-15: family marker (0x0800)
        bit  9    : new_state (0=off, 1=on)
        bit  8    : unit_index >= 256 high bit
        low byte  : unit index low 8 bits
    """
    word = 0x0800 | (unit_index & 0xFF)
    if unit_index >= 256:
        word |= 0x0100
    if new_state:
        word |= 0x0200
    return word & 0xFFFF


def _arming_changed_word(area_index: int, new_mode: int, user_index: int) -> int:
    """Encode a SECURITY_MODE_CHANGE catch-all event word.

    Layout (mirrors events._classify catch-all branch and clsText.cs:2155-2217):
        bits 12-14: SecurityMode (0..7)
        bits 8-11 : area index   (0 = system / no specific area)
        low byte  : user/code index that triggered the change (0 = panel)

    NOTE: the classifier in :func:`omni_pca.events._classify` only routes
    a word to ArmingChanged when ``(word >> 8) & 0xF0`` is non-zero. Our
    encoding satisfies that as long as ``new_mode`` is at least 1 (the
    SecurityMode high nibble of the high byte is non-zero). For Off (0)
    the test seeds a non-zero mode — Disarm (mode=Off) flowing through
    the same path would round-trip as an UnknownEvent, which matches
    real-panel behaviour where Off is pushed as a different event family.
    """
    word = ((new_mode & 0x07) << 12) | ((area_index & 0x0F) << 8) | (user_index & 0xFF)
    return word & 0xFFFF


# --------------------------------------------------------------------------
# Stock reply / NAK builders
# --------------------------------------------------------------------------


def _build_ack() -> Message:
    """Build a v2 Ack (opcode 1) with no payload."""
    return encode_v2(OmniLink2MessageType.Ack, b"")


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
