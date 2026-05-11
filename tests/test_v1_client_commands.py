"""Unit tests for the OmniClientV1 write methods.

These exercise wire-payload construction by monkey-patching the
connection's ``request`` method so we never have to open a UDP socket.
The contract under test:

* :meth:`OmniClientV1.execute_command` packs ``[cmd][p1][p2_hi][p2_lo]``.
* :meth:`OmniClientV1.execute_security_command` packs
  ``[area][mode][d1][d2][d3][d4]`` with the C# digit-by-digit form.
* Convenience wrappers (``turn_unit_on`` etc) route through
  :meth:`execute_command` with the right Command enum values.
* Replies are interpreted: Ack → return, Nak → CommandFailedError,
  non-zero SecurityCommandResponse → CommandFailedError with code.
"""

from __future__ import annotations

import struct

import pytest

from omni_pca.commands import (
    Command,
    CommandFailedError,
    SecurityCommandResponse,
)
from omni_pca.message import START_CHAR_V1_UNADDRESSED, Message
from omni_pca.models import SecurityMode
from omni_pca.opcodes import OmniLinkMessageType
from omni_pca.v1.client import OmniClientV1


class _FakeConn:
    """Records each request, returns a canned reply.

    Tests construct one with a list of (opcode, payload_bytes) replies in
    order; each call to :meth:`request` consumes one.
    """

    def __init__(
        self,
        replies: list[tuple[int, bytes]] | None = None,
    ) -> None:
        self.replies = replies or []
        self.calls: list[tuple[int, bytes]] = []

    async def request(
        self,
        opcode: int,
        payload: bytes = b"",
        timeout: float | None = None,
    ) -> Message:
        self.calls.append((int(opcode), bytes(payload)))
        if not self.replies:
            # Default: panel ack — works for the boring success path.
            return Message(
                start_char=START_CHAR_V1_UNADDRESSED,
                data=bytes([int(OmniLinkMessageType.Ack)]),
            )
        reply_op, reply_payload = self.replies.pop(0)
        return Message(
            start_char=START_CHAR_V1_UNADDRESSED,
            data=bytes([reply_op]) + reply_payload,
        )


def _make_client(replies: list[tuple[int, bytes]] | None = None) -> tuple[OmniClientV1, _FakeConn]:
    client = OmniClientV1(
        host="127.0.0.1",
        controller_key=b"\x00" * 16,
    )
    fake = _FakeConn(replies)
    # Swap out the real connection with our recorder.
    client._conn = fake  # type: ignore[assignment]
    return client, fake


# ---- execute_command ---------------------------------------------------


@pytest.mark.asyncio
async def test_execute_command_packs_payload_be() -> None:
    client, fake = _make_client()
    await client.execute_command(Command.UNIT_LEVEL, parameter1=42, parameter2=0x1234)
    assert len(fake.calls) == 1
    opcode, payload = fake.calls[0]
    assert opcode == int(OmniLinkMessageType.Command)
    # [cmd][p1][p2_hi][p2_lo]
    assert payload == struct.pack(">BBH", int(Command.UNIT_LEVEL), 42, 0x1234)


@pytest.mark.asyncio
async def test_execute_command_rejects_oversized_parameters() -> None:
    client, _ = _make_client()
    with pytest.raises(ValueError, match="parameter1"):
        await client.execute_command(Command.UNIT_LEVEL, parameter1=256, parameter2=1)
    with pytest.raises(ValueError, match="parameter2"):
        await client.execute_command(Command.UNIT_LEVEL, parameter1=0, parameter2=0x10000)


@pytest.mark.asyncio
async def test_execute_command_nak_raises_command_failed() -> None:
    client, _ = _make_client([(int(OmniLinkMessageType.Nak), b"")])
    with pytest.raises(CommandFailedError, match="NAK"):
        await client.execute_command(Command.UNIT_ON, parameter2=5)


@pytest.mark.asyncio
async def test_execute_command_unexpected_reply_raises() -> None:
    # Panel returns SystemInformation reply to a Command request — that's bogus.
    client, _ = _make_client(
        [(int(OmniLinkMessageType.SystemInformation), b"\x00")]
    )
    with pytest.raises(CommandFailedError, match="unexpected reply"):
        await client.execute_command(Command.UNIT_ON, parameter2=5)


# ---- thin wrappers -----------------------------------------------------


@pytest.mark.asyncio
async def test_turn_unit_on_sends_unit_on_command() -> None:
    client, fake = _make_client()
    await client.turn_unit_on(7)
    opcode, payload = fake.calls[0]
    assert opcode == int(OmniLinkMessageType.Command)
    assert payload[0] == int(Command.UNIT_ON)
    assert (payload[2] << 8) | payload[3] == 7


@pytest.mark.asyncio
async def test_turn_unit_off_sends_unit_off_command() -> None:
    client, fake = _make_client()
    await client.turn_unit_off(255)
    payload = fake.calls[0][1]
    assert payload[0] == int(Command.UNIT_OFF)
    assert (payload[2] << 8) | payload[3] == 255


@pytest.mark.asyncio
async def test_set_unit_level_packs_percent_as_p1() -> None:
    client, fake = _make_client()
    await client.set_unit_level(3, 75)
    payload = fake.calls[0][1]
    assert payload[0] == int(Command.UNIT_LEVEL)
    assert payload[1] == 75
    assert (payload[2] << 8) | payload[3] == 3


@pytest.mark.asyncio
async def test_set_unit_level_rejects_out_of_range_percent() -> None:
    client, _ = _make_client()
    with pytest.raises(ValueError, match="0..100"):
        await client.set_unit_level(1, 101)
    with pytest.raises(ValueError, match="0..100"):
        await client.set_unit_level(1, -1)


@pytest.mark.asyncio
async def test_bypass_zone_packs_code_as_p1_and_zone_as_p2() -> None:
    client, fake = _make_client()
    await client.bypass_zone(12, code=5)
    payload = fake.calls[0][1]
    assert payload[0] == int(Command.BYPASS_ZONE)
    assert payload[1] == 5
    assert (payload[2] << 8) | payload[3] == 12


@pytest.mark.asyncio
async def test_restore_zone_packs_code_and_zone() -> None:
    client, fake = _make_client()
    await client.restore_zone(99, code=3)
    payload = fake.calls[0][1]
    assert payload[0] == int(Command.RESTORE_ZONE)
    assert payload[1] == 3
    assert (payload[2] << 8) | payload[3] == 99


@pytest.mark.asyncio
async def test_execute_button() -> None:
    client, fake = _make_client()
    await client.execute_button(15)
    payload = fake.calls[0][1]
    assert payload[0] == int(Command.EXECUTE_BUTTON)
    assert (payload[2] << 8) | payload[3] == 15


@pytest.mark.asyncio
async def test_set_thermostat_modes_route_through_command() -> None:
    client, fake = _make_client()
    await client.set_thermostat_system_mode(2, 1)   # 1 = Heat
    await client.set_thermostat_fan_mode(2, 2)      # 2 = On
    await client.set_thermostat_hold_mode(2, 1)     # 1 = Hold
    cmds = [p[1][0] for p in fake.calls]
    assert cmds == [
        int(Command.SET_THERMOSTAT_SYSTEM_MODE),
        int(Command.SET_THERMOSTAT_FAN_MODE),
        int(Command.SET_THERMOSTAT_HOLD_MODE),
    ]


@pytest.mark.asyncio
async def test_set_thermostat_setpoint_raw_validates_byte_range() -> None:
    client, _ = _make_client()
    with pytest.raises(ValueError, match="raw_temp"):
        await client.set_thermostat_heat_setpoint_raw(1, 256)
    with pytest.raises(ValueError, match="raw_temp"):
        await client.set_thermostat_cool_setpoint_raw(1, -1)


# ---- execute_security_command ------------------------------------------


@pytest.mark.asyncio
async def test_execute_security_command_digit_packing() -> None:
    # Code 1234 → digits 1, 2, 3, 4.
    client, fake = _make_client([(int(OmniLinkMessageType.Ack), b"")])
    await client.execute_security_command(area=1, mode=SecurityMode.OFF, code=1234)
    opcode, payload = fake.calls[0]
    assert opcode == int(OmniLinkMessageType.ExecuteSecurityCommand)
    assert payload == bytes([1, int(SecurityMode.OFF), 1, 2, 3, 4])


@pytest.mark.asyncio
async def test_execute_security_command_pads_short_codes() -> None:
    # Code 7 → digits 0, 0, 0, 7.
    client, fake = _make_client([(int(OmniLinkMessageType.Ack), b"")])
    await client.execute_security_command(area=8, mode=SecurityMode.AWAY, code=7)
    payload = fake.calls[0][1]
    assert payload == bytes([8, int(SecurityMode.AWAY), 0, 0, 0, 7])


@pytest.mark.asyncio
async def test_execute_security_command_response_success_returns() -> None:
    # Panel returns ExecuteSecurityCommandResponse with status=0 (success).
    client, _ = _make_client(
        [(
            int(OmniLinkMessageType.ExecuteSecurityCommandResponse),
            bytes([int(SecurityCommandResponse.SUCCESS)]),
        )]
    )
    await client.execute_security_command(area=1, mode=SecurityMode.OFF, code=0)


@pytest.mark.asyncio
async def test_execute_security_command_response_failure_raises() -> None:
    # Panel returns ExecuteSecurityCommandResponse with status=
    # SecureSystem (1) — wrong code or area not enabled for this code.
    client, _ = _make_client(
        [(
            int(OmniLinkMessageType.ExecuteSecurityCommandResponse),
            bytes([int(SecurityCommandResponse.INVALID_CODE)]),
        )]
    )
    with pytest.raises(CommandFailedError) as ei:
        await client.execute_security_command(
            area=1, mode=SecurityMode.AWAY, code=9999
        )
    assert ei.value.failure_code == int(SecurityCommandResponse.INVALID_CODE)


@pytest.mark.asyncio
async def test_execute_security_command_nak_raises() -> None:
    client, _ = _make_client([(int(OmniLinkMessageType.Nak), b"")])
    with pytest.raises(CommandFailedError, match="NAK"):
        await client.execute_security_command(
            area=1, mode=SecurityMode.OFF, code=0
        )


@pytest.mark.asyncio
async def test_execute_security_command_rejects_bad_inputs() -> None:
    client, _ = _make_client()
    with pytest.raises(ValueError, match="area"):
        await client.execute_security_command(area=0, mode=SecurityMode.OFF, code=0)
    with pytest.raises(ValueError, match="code"):
        await client.execute_security_command(
            area=1, mode=SecurityMode.OFF, code=10000
        )


# ---- acknowledge_alerts -------------------------------------------------


@pytest.mark.asyncio
async def test_acknowledge_alerts_is_noop_on_v1() -> None:
    """v1 has no AcknowledgeAlerts opcode — method should not call request."""
    client, fake = _make_client()
    await client.acknowledge_alerts()
    assert fake.calls == []
