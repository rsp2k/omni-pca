"""End-to-end wire round-trip: client → MockPanel → program decoded.

Seeds the MockPanel with a known :class:`Program`, drives the v2
``UploadProgram`` opcode through a real TCP socket, and asserts the
decoded round-trip equals the seeded Program. Proves the on-the-wire
framing (2-byte BE ProgramNumber header + 14-byte body wrapped in a
``ProgramData`` reply) lines up with our decoder.
"""

from __future__ import annotations

import struct

import pytest

from omni_pca.connection import OmniConnection
from omni_pca.mock_panel import MockPanel, MockState
from omni_pca.opcodes import OmniLink2MessageType
from omni_pca.programs import Days, Program, ProgramType

CONTROLLER_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f")


def _seeded() -> Program:
    """A TIMED program with non-trivial fields in every slot.

    Picks values that would fail if any byte got swapped or zeroed.
    """
    return Program(
        slot=42,
        prog_type=int(ProgramType.TIMED),
        cond=0x8D09,
        cond2=0x9B09,
        cmd=0x44,
        par=3,
        pr2=0x0100,
        month=8,
        day=12,
        days=int(Days.MONDAY | Days.TUESDAY | Days.WEDNESDAY | Days.THURSDAY | Days.FRIDAY),
        hour=7,
        minute=15,
    )


@pytest.mark.asyncio
async def test_v2_upload_program_round_trips_through_mock_panel() -> None:
    seeded = _seeded()
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={42: seeded.encode_wire_bytes()}),
    )
    async with (
        panel.serve(transport="tcp") as (host, port),
        OmniConnection(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as conn,
    ):
        # UploadProgram request body: [number_hi, number_lo, request_reason]
        payload = struct.pack(">HB", 42, 0)
        reply = await conn.request(OmniLink2MessageType.UploadProgram, payload)
        assert reply.opcode == int(OmniLink2MessageType.ProgramData)

        # Reply payload: [number_hi, number_lo] + 14-byte body
        assert len(reply.payload) == 2 + 14
        echoed_number = (reply.payload[0] << 8) | reply.payload[1]
        assert echoed_number == 42

        decoded = Program.from_wire_bytes(reply.payload[2:], slot=42)

    # Compare field-by-field — slot was passed through unchanged.
    assert decoded.prog_type == seeded.prog_type
    assert decoded.cond == seeded.cond
    assert decoded.cond2 == seeded.cond2
    assert decoded.cmd == seeded.cmd
    assert decoded.par == seeded.par
    assert decoded.pr2 == seeded.pr2
    assert decoded.month == seeded.month
    assert decoded.day == seeded.day
    assert decoded.days == seeded.days
    assert decoded.hour == seeded.hour
    assert decoded.minute == seeded.minute


@pytest.mark.asyncio
async def test_v2_upload_program_empty_slot_returns_zero_body() -> None:
    """An unseeded slot should respond with 14 zero bytes (matches real panel)."""
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with (
        panel.serve(transport="tcp") as (host, port),
        OmniConnection(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as conn,
    ):
        payload = struct.pack(">HB", 99, 0)
        reply = await conn.request(OmniLink2MessageType.UploadProgram, payload)
        assert reply.opcode == int(OmniLink2MessageType.ProgramData)
        assert reply.payload == bytes([0, 99]) + b"\x00" * 14
        decoded = Program.from_wire_bytes(reply.payload[2:], slot=99)
        assert decoded.is_empty()


@pytest.mark.asyncio
async def test_v2_upload_program_event_type_no_swap_on_wire() -> None:
    """EVENT-typed programs must NOT swap Mon/Day on the wire (clsOLMsgProgramData
    doesn't apply the file-layout swap)."""
    seeded = Program(
        slot=7,
        prog_type=int(ProgramType.EVENT),
        cond=0x0C04,
        cmd=int(OmniLink2MessageType.Ack),  # arbitrary; just non-zero
        month=5,    # in WIRE layout: byte 9 = month, byte 10 = day
        day=12,
    )
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={7: seeded.encode_wire_bytes()}),
    )
    async with (
        panel.serve(transport="tcp") as (host, port),
        OmniConnection(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as conn,
    ):
        payload = struct.pack(">HB", 7, 0)
        reply = await conn.request(OmniLink2MessageType.UploadProgram, payload)
        body = reply.payload[2:]
        # Byte 9 should be 5 (month), byte 10 should be 12 (day) -- the
        # exact wire-layout encoding of an EVENT program with month=5,
        # day=12. If the mock swapped (treating it as file layout), we'd
        # see byte 9 = 12 and byte 10 = 5.
        assert body[9] == 5
        assert body[10] == 12
        # And the decoded values match what we seeded.
        decoded = Program.from_wire_bytes(body, slot=7)
        assert decoded.month == 5
        assert decoded.day == 12
