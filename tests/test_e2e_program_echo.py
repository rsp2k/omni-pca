"""End-to-end wire round-trip: client → MockPanel → program decoded.

Seeds the MockPanel with known :class:`Program` records, exercises
both wire dialects, and asserts the decoded result equals what was
seeded.

* v2 (TCP, request/response per slot): drives ``UploadProgram`` once
  per slot. Proves the per-program framing (2-byte BE ProgramNumber +
  14-byte body wrapped in a ``ProgramData`` reply).
* v1 (UDP, streaming): drives bare ``UploadPrograms``, ack-walks the
  streamed ``ProgramData`` replies to ``EOD``. Proves the streaming
  lock-step matches the panel's behaviour described in
  ``clsHAC.OL1ReadConfig`` (clsHAC.cs:4403, 4538-4540, 4642-4651).
"""

from __future__ import annotations

import struct

import pytest

from omni_pca.connection import OmniConnection
from omni_pca.mock_panel import MockPanel, MockState
from omni_pca.opcodes import OmniLink2MessageType, OmniLinkMessageType
from omni_pca.programs import Days, Program, ProgramType
from omni_pca.v1 import OmniClientV1

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


# ---- v1 streaming -------------------------------------------------------


def _decode_v1_programdata(payload: bytes) -> tuple[int, Program]:
    """Strip the BE ProgramNumber prefix from a v1 ``ProgramData`` payload,
    decode the 14-byte body. Mirrors the v2 helper inline above."""
    assert len(payload) >= 2 + 14
    slot = (payload[0] << 8) | payload[1]
    return slot, Program.from_wire_bytes(payload[2 : 2 + 14], slot=slot)


@pytest.mark.asyncio
async def test_v1_upload_programs_streams_all_seeded_slots() -> None:
    """The v1 ``UploadPrograms`` opcode is bare; the panel streams one
    ``ProgramData`` reply per defined slot, each followed by a client Ack,
    terminated by ``EOD``. Order is by ascending slot index — which is
    what we feed back from ``sorted(state.programs)``."""
    seeded = {
        12: Program(slot=12, prog_type=int(ProgramType.TIMED), cmd=3, hour=6, minute=0,
                    days=int(Days.MONDAY | Days.FRIDAY)),
        42: Program(slot=42, prog_type=int(ProgramType.TIMED), cond=0x8D09, cond2=0x9B09,
                    cmd=0x44, par=3, pr2=0x0100, month=8, day=12,
                    days=int(Days.MONDAY), hour=7, minute=15),
        99: Program(slot=99, prog_type=int(ProgramType.EVENT), cmd=5, month=5, day=12),
    }
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={s: p.encode_wire_bytes() for s, p in seeded.items()}),
    )
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            received: dict[int, Program] = {}
            async for reply in c.connection.iter_streaming(
                OmniLinkMessageType.UploadPrograms
            ):
                assert reply.opcode == int(OmniLinkMessageType.ProgramData)
                slot, prog = _decode_v1_programdata(reply.payload)
                received[slot] = prog

    assert set(received) == set(seeded)
    for slot, want in seeded.items():
        got = received[slot]
        # Field-by-field — same checks as the v2 test, plus a slot equality.
        assert got.slot == slot
        assert got.prog_type == want.prog_type
        assert got.cond == want.cond
        assert got.cond2 == want.cond2
        assert got.cmd == want.cmd
        assert got.par == want.par
        assert got.pr2 == want.pr2
        assert got.month == want.month
        assert got.day == want.day
        assert got.days == want.days
        assert got.hour == want.hour
        assert got.minute == want.minute


@pytest.mark.asyncio
async def test_v1_upload_programs_empty_state_yields_immediate_eod() -> None:
    """No programs defined → the streaming iterator terminates without
    yielding anything (the panel jumps straight to EOD)."""
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            replies = [
                r async for r in c.connection.iter_streaming(
                    OmniLinkMessageType.UploadPrograms
                )
            ]
    assert replies == []


# ---- v2 iter_programs (reason=1 "next defined" iteration) ---------------


@pytest.mark.asyncio
async def test_v2_upload_program_reason1_returns_next_defined_slot() -> None:
    """``request_reason=1`` should return the lowest defined slot strictly
    greater than the requested number — the C# panel uses this to iterate
    (clsHAC.cs:5331)."""
    seeded = {
        5: Program(slot=5, prog_type=int(ProgramType.TIMED), cmd=3),
        12: Program(slot=12, prog_type=int(ProgramType.TIMED), cmd=3),
        99: Program(slot=99, prog_type=int(ProgramType.EVENT), cmd=5),
    }
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={s: p.encode_wire_bytes() for s, p in seeded.items()}),
    )
    async with (
        panel.serve(transport="tcp") as (host, port),
        OmniConnection(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as conn,
    ):
        # Seed slot 0 with reason=1 → first defined slot (5).
        reply = await conn.request(
            OmniLink2MessageType.UploadProgram, struct.pack(">HB", 0, 1)
        )
        assert reply.opcode == int(OmniLink2MessageType.ProgramData)
        assert (reply.payload[0] << 8) | reply.payload[1] == 5

        # From slot 5 with reason=1 → slot 12.
        reply = await conn.request(
            OmniLink2MessageType.UploadProgram, struct.pack(">HB", 5, 1)
        )
        assert (reply.payload[0] << 8) | reply.payload[1] == 12

        # From slot 12 with reason=1 → slot 99.
        reply = await conn.request(
            OmniLink2MessageType.UploadProgram, struct.pack(">HB", 12, 1)
        )
        assert (reply.payload[0] << 8) | reply.payload[1] == 99

        # From slot 99 with reason=1 → EOD (no more).
        reply = await conn.request(
            OmniLink2MessageType.UploadProgram, struct.pack(">HB", 99, 1)
        )
        assert reply.opcode == int(OmniLink2MessageType.EOD)


@pytest.mark.asyncio
async def test_v2_client_iter_programs_enumerates_all_seeded() -> None:
    """High-level OmniClient.iter_programs() drives the reason=1 iteration
    and yields decoded Program records in slot-ascending order."""
    from omni_pca.client import OmniClient
    seeded = {
        12: Program(slot=12, prog_type=int(ProgramType.TIMED), cmd=3, hour=6, minute=0,
                    days=int(Days.MONDAY | Days.FRIDAY)),
        42: Program(slot=42, prog_type=int(ProgramType.TIMED), cond=0x8D09, cond2=0x9B09,
                    cmd=0x44, par=3, pr2=0x0100, month=8, day=12,
                    days=int(Days.MONDAY), hour=7, minute=15),
        99: Program(slot=99, prog_type=int(ProgramType.EVENT), cmd=5, month=5, day=12),
    }
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={s: p.encode_wire_bytes() for s, p in seeded.items()}),
    )
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            received = [p async for p in c.iter_programs()]

    assert [p.slot for p in received] == [12, 42, 99]
    for got, want in zip(received, seeded.values()):
        assert got.prog_type == want.prog_type
        assert got.cmd == want.cmd
        assert got.hour == want.hour
        assert got.minute == want.minute


@pytest.mark.asyncio
async def test_v2_client_iter_programs_empty_state_yields_nothing() -> None:
    from omni_pca.client import OmniClient
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            received = [p async for p in c.iter_programs()]
    assert received == []


# ---- v1 client iter_programs (high-level wrapper over iter_streaming) ----


@pytest.mark.asyncio
async def test_mockstate_from_pca_serves_real_panel_programs() -> None:
    """End-to-end: build MockState from the live .pca, drive iter_programs
    over v2 wire, decode every yielded Program. This exercises the full
    file → mock → wire → decoder pipeline with real on-disk data.

    The fixture is the same plain-text dump tests/test_pca_file.py uses;
    we re-encrypt with KEY_EXPORT on the fly so parse_pca_file accepts it.
    """
    from pathlib import Path

    from omni_pca.client import OmniClient
    from omni_pca.mock_panel import MockState
    from omni_pca.pca_file import KEY_EXPORT, decrypt_pca_bytes

    plain = Path("/home/kdm/home-auto/HAI/pca-re/extracted/Our_House.pca.plain")
    if not plain.is_file():
        pytest.skip(f"live fixture missing: {plain}")
    encrypted = decrypt_pca_bytes(plain.read_bytes(), KEY_EXPORT)

    state = MockState.from_pca(encrypted, key=KEY_EXPORT)
    # SystemInfo fields were populated from the .pca header.
    assert state.model_byte == 16          # OMNI_PRO_II
    assert state.firmware_major == 2
    # Programs: 330 defined per Phase 1 recon.
    assert len(state.programs) == 330

    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            decoded = [p async for p in c.iter_programs()]

    # Every defined slot streamed back, in ascending slot order.
    assert len(decoded) == 330
    assert [p.slot for p in decoded] == sorted(state.programs)
    # Spot check: every decoded record has a known ProgramType.
    for p in decoded:
        assert p.prog_type in {1, 2, 3}  # TIMED / EVENT / YEARLY from this fixture


@pytest.mark.asyncio
async def test_v1_client_iter_programs_enumerates_all_seeded() -> None:
    seeded = {
        12: Program(slot=12, prog_type=int(ProgramType.TIMED), cmd=3, hour=6, minute=0,
                    days=int(Days.MONDAY | Days.FRIDAY)),
        42: Program(slot=42, prog_type=int(ProgramType.TIMED), cond=0x8D09, cond2=0x9B09,
                    cmd=0x44, par=3, pr2=0x0100, month=8, day=12,
                    days=int(Days.MONDAY), hour=7, minute=15),
        99: Program(slot=99, prog_type=int(ProgramType.EVENT), cmd=5, month=5, day=12),
    }
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={s: p.encode_wire_bytes() for s, p in seeded.items()}),
    )
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            received = [p async for p in c.iter_programs()]

    assert [p.slot for p in received] == [12, 42, 99]
    for got, want in zip(received, seeded.values()):
        assert got.prog_type == want.prog_type
        assert got.cmd == want.cmd
        assert got.cond == want.cond
        assert got.cond2 == want.cond2
        assert got.hour == want.hour
        assert got.minute == want.minute
