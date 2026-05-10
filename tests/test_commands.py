"""Unit tests for command opcodes and status range queries.

The tests use a captured-payload approach: we monkey-patch
``OmniClient._conn.request`` so it records the (opcode, payload) pair
that the client would emit and returns whatever canned ``Message``
the test wants. No network involved — just round-trip the bytes.

Conventions:
    * Each test pins the exact wire bytes the client should produce, so
      that any future refactor that rearranges the payload layout is
      caught immediately.
    * Where a single command can be expressed as a high-level helper
      (``turn_unit_on``) we still verify the underlying ``Command``
      enum value and the param1/param2 byte placement, not just the
      success path.
"""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from omni_pca.client import OmniClient
from omni_pca.commands import Command, CommandFailedError, SecurityCommandResponse
from omni_pca.message import Message, encode_v2
from omni_pca.models import (
    AreaStatus,
    FanMode,
    HoldMode,
    HvacMode,
    ObjectType,
    SecurityMode,
    ThermostatStatus,
    UnitStatus,
    ZoneStatus,
)
from omni_pca.opcodes import OmniLink2MessageType

# --------------------------------------------------------------------------
# Test scaffolding: a stub OmniClient that captures requests instead of
# sending them. We bypass __init__ (which builds an OmniConnection) by
# using object.__new__ + manually setting the _conn attr to our stub.
# --------------------------------------------------------------------------


@dataclass
class _RecordedRequest:
    opcode: int
    payload: bytes


class _StubConn:
    """Stand-in for OmniConnection; .request() captures + returns a canned reply."""

    def __init__(
        self,
        reply_factory: Callable[[int, bytes], Message] | None = None,
    ) -> None:
        self.calls: list[_RecordedRequest] = []
        self._reply_factory = reply_factory or self._default_ack

    @staticmethod
    def _default_ack(_opcode: int, _payload: bytes) -> Message:
        return encode_v2(OmniLink2MessageType.Ack)

    async def request(
        self,
        opcode: OmniLink2MessageType | int,
        payload: bytes = b"",
        timeout: float | None = None,
    ) -> Message:
        del timeout  # mirror the OmniConnection.request signature; unused here
        op_int = int(opcode)
        self.calls.append(_RecordedRequest(opcode=op_int, payload=bytes(payload)))
        return self._reply_factory(op_int, bytes(payload))


def _make_client(stub: _StubConn) -> OmniClient:
    """Build an OmniClient with a stubbed connection (no socket, no handshake)."""
    client = object.__new__(OmniClient)
    client._conn = stub  # type: ignore[attr-defined]
    client._subscriber_task = None  # type: ignore[attr-defined]
    return client


# --------------------------------------------------------------------------
# Command enum value pins. These guard against accidental renumbering.
# --------------------------------------------------------------------------


def test_command_enum_pins_unit_values() -> None:
    assert Command.UNIT_OFF == 0
    assert Command.UNIT_ON == 1
    assert Command.UNIT_LEVEL == 9
    assert Command.BYPASS_ZONE == 4
    assert Command.RESTORE_ZONE == 5


def test_command_enum_pins_thermostat_values() -> None:
    # enuUnitCommand.SetLowSetPt (line 71) → 66 (heat)
    assert Command.SET_THERMOSTAT_HEAT_SETPOINT == 66
    # enuUnitCommand.SetHighSetPt (line 72) → 67 (cool)
    assert Command.SET_THERMOSTAT_COOL_SETPOINT == 67
    # enuUnitCommand.Mode/Fan/Hold (lines 73/74/75)
    assert Command.SET_THERMOSTAT_SYSTEM_MODE == 68
    assert Command.SET_THERMOSTAT_FAN_MODE == 69
    assert Command.SET_THERMOSTAT_HOLD_MODE == 70


def test_command_enum_pins_message_and_program_values() -> None:
    assert Command.SHOW_MESSAGE_WITH_BEEP == 80
    assert Command.LOG_MESSAGE == 81
    assert Command.CLEAR_MESSAGE == 82
    assert Command.SHOW_MESSAGE_NO_BEEP == 86
    assert Command.EXECUTE_BUTTON == 7
    assert Command.EXECUTE_PROGRAM == 104


def test_security_command_response_enum_pins() -> None:
    assert SecurityCommandResponse.SUCCESS == 0
    assert SecurityCommandResponse.INVALID_CODE == 1
    assert SecurityCommandResponse.INVALID_AREA == 3
    assert SecurityCommandResponse.CODE_LOCKED_OUT == 6


# --------------------------------------------------------------------------
# execute_command() — generic Command opcode (20)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_command_payload_layout() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    # Pretend we're flipping unit #257 ON (parameter2 needs both bytes).
    await client.execute_command(Command.UNIT_ON, parameter1=0, parameter2=257)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call.opcode == int(OmniLink2MessageType.Command)
    # Command (1) + p1 (1 byte) + p2 (BE u16) = 4 bytes
    assert call.payload == bytes([1, 0, 0x01, 0x01])


@pytest.mark.asyncio
async def test_execute_command_packs_param1_byte_and_param2_be_u16() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.execute_command(
        Command.UNIT_LEVEL, parameter1=75, parameter2=0xABCD
    )
    payload = stub.calls[0].payload
    # [cmd=9, p1=75, p2_hi=0xAB, p2_lo=0xCD]
    assert payload == bytes([9, 75, 0xAB, 0xCD])


@pytest.mark.asyncio
async def test_execute_command_validates_param_ranges() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    with pytest.raises(ValueError, match="parameter1"):
        await client.execute_command(Command.UNIT_ON, parameter1=256)
    with pytest.raises(ValueError, match="parameter2"):
        await client.execute_command(Command.UNIT_ON, parameter2=0x10000)
    # No request emitted on validation failure.
    assert stub.calls == []


@pytest.mark.asyncio
async def test_execute_command_raises_on_nak() -> None:
    def nak_reply(_op: int, _pl: bytes) -> Message:
        return encode_v2(OmniLink2MessageType.Nak)

    stub = _StubConn(reply_factory=nak_reply)
    client = _make_client(stub)
    with pytest.raises(CommandFailedError, match="NAK"):
        await client.execute_command(Command.UNIT_ON, parameter2=1)


# --------------------------------------------------------------------------
# Convenience wrappers over execute_command
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_unit_on_off_emits_correct_command_byte() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.turn_unit_on(5)
    await client.turn_unit_off(5)
    assert stub.calls[0].payload == bytes([1, 0, 0, 5])  # UNIT_ON
    assert stub.calls[1].payload == bytes([0, 0, 0, 5])  # UNIT_OFF


@pytest.mark.asyncio
async def test_set_unit_level_validates_range_and_emits_level() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.set_unit_level(3, 50)
    assert stub.calls[0].payload == bytes([9, 50, 0, 3])
    with pytest.raises(ValueError, match=r"0\.\.100"):
        await client.set_unit_level(3, 101)


@pytest.mark.asyncio
async def test_bypass_and_restore_zone_emit_correct_payload() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.bypass_zone(12, code=2)
    await client.restore_zone(12, code=2)
    assert stub.calls[0].payload == bytes([4, 2, 0, 12])  # BYPASS_ZONE
    assert stub.calls[1].payload == bytes([5, 2, 0, 12])  # RESTORE_ZONE


@pytest.mark.asyncio
async def test_set_thermostat_modes_emit_correct_payloads() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.set_thermostat_system_mode(2, HvacMode.COOL)
    await client.set_thermostat_fan_mode(2, FanMode.ON)
    await client.set_thermostat_hold_mode(2, HoldMode.HOLD)
    assert stub.calls[0].payload == bytes([68, int(HvacMode.COOL), 0, 2])
    assert stub.calls[1].payload == bytes([69, int(FanMode.ON), 0, 2])
    assert stub.calls[2].payload == bytes([70, int(HoldMode.HOLD), 0, 2])


@pytest.mark.asyncio
async def test_set_thermostat_setpoints_use_raw_byte() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.set_thermostat_heat_setpoint_raw(1, 140)  # ~70 °F
    await client.set_thermostat_cool_setpoint_raw(1, 160)  # ~80 °F
    assert stub.calls[0].payload == bytes([66, 140, 0, 1])
    assert stub.calls[1].payload == bytes([67, 160, 0, 1])


@pytest.mark.asyncio
async def test_button_program_message_helpers() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.execute_button(4)
    await client.execute_program(7)
    await client.show_message(2, beep=True)
    await client.show_message(2, beep=False)
    await client.clear_message(2)
    assert stub.calls[0].payload == bytes([7, 0, 0, 4])  # EXECUTE_BUTTON
    assert stub.calls[1].payload == bytes([104, 0, 0, 7])  # EXECUTE_PROGRAM
    assert stub.calls[2].payload == bytes([80, 0, 0, 2])  # SHOW_MSG_BEEP
    assert stub.calls[3].payload == bytes([86, 0, 0, 2])  # SHOW_MSG_NOBEEP
    assert stub.calls[4].payload == bytes([82, 0, 0, 2])  # CLEAR_MESSAGE


# --------------------------------------------------------------------------
# execute_security_command (opcode 74)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_security_command_payload_encoding_away_1234() -> None:
    """The C# code packs the 4-digit code as four separate digit bytes."""
    stub = _StubConn(
        reply_factory=lambda _op, _pl: encode_v2(
            OmniLink2MessageType.ExecuteSecurityCommandResponse,
            bytes([SecurityCommandResponse.SUCCESS]),
        )
    )
    client = _make_client(stub)
    result = await client.execute_security_command(
        area=1, mode=SecurityMode.AWAY, code=1234
    )
    assert result is None
    payload = stub.calls[0].payload
    # area, mode, d1, d2, d3, d4
    assert payload == bytes([1, int(SecurityMode.AWAY), 1, 2, 3, 4])
    assert stub.calls[0].opcode == int(
        OmniLink2MessageType.ExecuteSecurityCommand
    )


@pytest.mark.asyncio
async def test_execute_security_command_pads_short_codes_with_zeros() -> None:
    """Code 7 → digits 0,0,0,7 (matches the C# arithmetic)."""
    stub = _StubConn(
        reply_factory=lambda _op, _pl: encode_v2(OmniLink2MessageType.Ack)
    )
    client = _make_client(stub)
    await client.execute_security_command(
        area=2, mode=SecurityMode.OFF, code=7
    )
    assert stub.calls[0].payload == bytes([2, 0, 0, 0, 0, 7])


@pytest.mark.asyncio
async def test_execute_security_command_failure_raises_with_code() -> None:
    def reply(_op: int, _pl: bytes) -> Message:
        return encode_v2(
            OmniLink2MessageType.ExecuteSecurityCommandResponse,
            bytes([SecurityCommandResponse.INVALID_CODE]),
        )

    stub = _StubConn(reply_factory=reply)
    client = _make_client(stub)
    with pytest.raises(CommandFailedError) as ei:
        await client.execute_security_command(
            area=1, mode=SecurityMode.AWAY, code=9999
        )
    assert ei.value.failure_code == int(SecurityCommandResponse.INVALID_CODE)
    assert "INVALID_CODE" in str(ei.value)


@pytest.mark.asyncio
async def test_execute_security_command_validates_inputs() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    with pytest.raises(ValueError, match="area"):
        await client.execute_security_command(
            area=0, mode=SecurityMode.AWAY, code=1234
        )
    with pytest.raises(ValueError, match="code"):
        await client.execute_security_command(
            area=1, mode=SecurityMode.AWAY, code=10000
        )
    assert stub.calls == []


# --------------------------------------------------------------------------
# acknowledge_alerts (opcode 60)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_alerts_sends_no_payload_and_expects_ack() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    await client.acknowledge_alerts()
    assert stub.calls[0].opcode == int(OmniLink2MessageType.AcknowledgeAlerts)
    assert stub.calls[0].payload == b""


# --------------------------------------------------------------------------
# get_object_status (opcode 34/35) — request payload + reply parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_object_status_builds_request_payload() -> None:
    """RequestStatus is [object_type, start_hi, start_lo, end_hi, end_lo]."""
    captured: list[bytes] = []

    def reply(_op: int, payload: bytes) -> Message:
        captured.append(payload)
        # Reply with one zone record: number=3, status=0x10, loop=200.
        body = bytes([int(ObjectType.ZONE), 0, 3, 0x10, 200])
        return encode_v2(OmniLink2MessageType.Status, body)

    stub = _StubConn(reply_factory=reply)
    client = _make_client(stub)
    zones = await client.get_object_status(ObjectType.ZONE, 3)
    assert captured[0] == struct.pack(">BHH", int(ObjectType.ZONE), 3, 3)
    assert len(zones) == 1
    z = zones[0]
    assert isinstance(z, ZoneStatus)
    assert z.index == 3
    assert z.raw_status == 0x10
    assert z.loop == 200


@pytest.mark.asyncio
async def test_get_object_status_parses_multiple_unit_records() -> None:
    """Unit records are 5 bytes each (clsOL2MsgStatus.cs:17)."""

    def reply(_op: int, _pl: bytes) -> Message:
        # object_type byte + two 5-byte unit records.
        records = (
            bytes([0, 1, 1, 0, 0])  # unit 1, state=1 (On)
            + bytes([0, 2, 100, 0, 0])  # unit 2, state=100 (level 0%)
        )
        return encode_v2(
            OmniLink2MessageType.Status,
            bytes([int(ObjectType.UNIT)]) + records,
        )

    stub = _StubConn(reply_factory=reply)
    client = _make_client(stub)
    units = await client.get_object_status(ObjectType.UNIT, 1, 2)
    assert len(units) == 2
    assert all(isinstance(u, UnitStatus) for u in units)
    u1, u2 = units
    assert u1.index == 1
    assert u1.state == 1
    assert u2.index == 2
    assert u2.state == 100


@pytest.mark.asyncio
async def test_get_object_status_returns_empty_on_eod() -> None:
    stub = _StubConn(
        reply_factory=lambda _op, _pl: encode_v2(OmniLink2MessageType.EOD)
    )
    client = _make_client(stub)
    out = await client.get_object_status(ObjectType.AREA, 99)
    assert out == []


@pytest.mark.asyncio
async def test_get_object_status_raises_on_nak() -> None:
    stub = _StubConn(
        reply_factory=lambda _op, _pl: encode_v2(OmniLink2MessageType.Nak)
    )
    client = _make_client(stub)
    with pytest.raises(CommandFailedError, match="NAK"):
        await client.get_object_status(ObjectType.ZONE, 1)


# --------------------------------------------------------------------------
# get_extended_status (opcode 58/59) — header has object_length byte
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_extended_status_request_layout_matches_spec() -> None:
    captured: list[bytes] = []

    def reply(_op: int, payload: bytes) -> Message:
        captured.append(payload)
        # Reply: object_type, object_length=4, then one zone record (4 bytes).
        body = bytes([int(ObjectType.ZONE), 4]) + bytes([0, 5, 0x00, 100])
        return encode_v2(OmniLink2MessageType.ExtendedStatus, body)

    stub = _StubConn(reply_factory=reply)
    client = _make_client(stub)
    zones = await client.get_extended_status(ObjectType.ZONE, 5, 5)
    assert captured[0] == struct.pack(">BHH", int(ObjectType.ZONE), 5, 5)
    assert stub.calls[0].opcode == int(
        OmniLink2MessageType.RequestExtendedStatus
    )
    assert len(zones) == 1
    assert zones[0].index == 5  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_get_extended_status_uses_object_length_byte_for_record_size() -> None:
    """ExtendedStatus thermostat record is 14 bytes (clsOL2MsgExtendedStatus.cs:138-235)."""
    record = bytes(
        [
            0, 1,        # number = 1
            0,           # status
            140,         # current temp raw
            120, 160,    # heat / cool setpoints
            int(HvacMode.AUTO),
            int(FanMode.AUTO),
            int(HoldMode.OFF),
            150,         # humidity raw
            120, 160,    # humidify / dehumidify setpoints
            130,         # outdoor temp raw
            1,           # H or C status
        ]
    )
    assert len(record) == 14

    def reply(_op: int, _pl: bytes) -> Message:
        body = bytes([int(ObjectType.THERMOSTAT), 14]) + record
        return encode_v2(OmniLink2MessageType.ExtendedStatus, body)

    stub = _StubConn(reply_factory=reply)
    client = _make_client(stub)
    out = await client.get_extended_status(ObjectType.THERMOSTAT, 1)
    assert len(out) == 1
    t = out[0]
    assert isinstance(t, ThermostatStatus)
    assert t.index == 1
    assert t.temperature_raw == 140
    assert t.system_mode == int(HvacMode.AUTO)


@pytest.mark.asyncio
async def test_get_extended_status_returns_empty_on_eod() -> None:
    stub = _StubConn(
        reply_factory=lambda _op, _pl: encode_v2(OmniLink2MessageType.EOD)
    )
    client = _make_client(stub)
    out = await client.get_extended_status(ObjectType.AREA, 1, 8)
    assert out == []


@pytest.mark.asyncio
async def test_get_extended_status_area_record_parses_to_areastatus() -> None:
    """Area ExtendedStatus record is 6 bytes
    (clsOL2MsgExtendedStatus.cs:75-118): number(2) + mode + alarms + entry +
    exit (matches our AreaStatus.parse).
    """

    def reply(_op: int, _pl: bytes) -> Message:
        # area 1, mode AWAY, alarms 0, entry 0, exit 30
        record = bytes([0, 1, int(SecurityMode.AWAY), 0, 0, 30])
        body = bytes([int(ObjectType.AREA), 6]) + record
        return encode_v2(OmniLink2MessageType.ExtendedStatus, body)

    stub = _StubConn(reply_factory=reply)
    client = _make_client(stub)
    out = await client.get_extended_status(ObjectType.AREA, 1)
    assert len(out) == 1
    a = out[0]
    assert isinstance(a, AreaStatus)
    assert a.index == 1
    assert a.mode == int(SecurityMode.AWAY)
    assert a.exit_timer_secs == 30


@pytest.mark.asyncio
async def test_get_object_status_validates_range() -> None:
    stub = _StubConn()
    client = _make_client(stub)
    with pytest.raises(ValueError, match="end"):
        await client.get_object_status(ObjectType.ZONE, 5, 3)
    assert stub.calls == []
