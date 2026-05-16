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
    # Names: per the live fixture's reconnaissance dump.
    assert len(state.zones) == 16
    assert len(state.units) == 44
    assert len(state.buttons) == 16
    assert len(state.thermostats) == 2
    # Areas: this fixture has no user-assigned names but
    # NumAreasUsed=1, so MockState.from_pca synthesizes a single
    # unnamed area 1 with the .pca's entry/exit delays.
    assert len(state.areas) == 1
    assert state.areas[1].name == ""
    assert state.areas[1].entry_delay == 60  # configured in PC Access
    assert state.areas[1].exit_delay == 90
    assert state.areas[1].enabled is True

    # Sanity-check the raw PcaAccount scalars too.
    from omni_pca.pca_file import parse_pca_file
    acct = parse_pca_file(encrypted, key=KEY_EXPORT)
    assert acct.temp_format == 1       # 1 = Fahrenheit
    assert acct.num_areas_used == 1
    assert acct.area_entry_delays[1] == 60
    assert acct.area_exit_delays[1] == 90

    # Area-1 boolean flags (real homeowner-configured values):
    #   EntryChime    OFF   (no keypad chime on entry)
    #   QuickArm      ON    (arming without a code)
    #   AutoBypass    OFF
    #   AllOnForAlarm ON
    #   TroubleBeep   OFF
    #   PerimeterChime OFF  (homeowner disabled)
    #   AudibleExitDelay ON
    assert acct.area_entry_chime[1] is False
    assert acct.area_quick_arm[1] is True
    assert acct.area_auto_bypass[1] is False
    assert acct.area_all_on_for_alarm[1] is True
    assert acct.area_trouble_beep[1] is False
    assert acct.area_perimeter_chime[1] is False
    assert acct.area_audible_exit_delay[1] is True
    # And the values flowed through MockState.
    assert state.areas[1].quick_arm is True
    assert state.areas[1].entry_chime is False
    assert state.areas[1].perimeter_chime is False

    # DST configuration — US default (Mar/2nd Sun, Nov/1st Sun).
    assert acct.dst_start_month == 3
    assert acct.dst_start_week == 2
    assert acct.dst_end_month == 11
    assert acct.dst_end_week == 1

    # Unit type derivation — X10 sub-types resolved via HouseCodeFormat.
    # HouseCode 1 in this fixture is HLC (5), so units 1..16 split into
    # HLCRoom (Number-1 ≡ 0 mod 8) and HLCLoad. HouseCodes 2..16 are
    # Extended (1), so units 17..256 are enuOL2UnitType.Extended (2).
    assert acct.unit_types[1] == 5       # ROOM ONE → HLCRoom
    assert acct.unit_types[2] == 6       # FRONT PORCH → HLCLoad
    assert acct.unit_types[9] == 5       # next room-slot → HLCRoom
    assert acct.unit_types[17] == 2      # HouseCode 2 Extended → Extended
    assert acct.unit_types[257] == 13    # ExpEnc → Output
    assert acct.unit_types[385] == 13    # VoltOut → Output
    assert acct.unit_types[393] == 12    # FlagOut → Flag
    # Unit type/areas threaded into MockUnitState — first 16 units are
    # under HouseCode 1 (HLC).
    assert state.units[1].unit_type == 5  # ROOM ONE → HLCRoom
    # Area was 0xff (panel default = "all") → normalized to 0x01 in mock.
    assert state.units[1].areas == 0x01

    # HouseCodes.EnableExtCode raw bytes.
    assert acct.house_code_formats[1] == 5    # HLC
    assert all(v == 1 for v in (
        acct.house_code_formats[i] for i in range(2, 17)
    ))  # all Extended

    # TimeClock 1: outdoor-lights schedule On 22:30 → Off 06:00 daily.
    tc1_on, tc1_off = acct.time_clocks[0], acct.time_clocks[1]
    assert (tc1_on.hour, tc1_on.minute) == (22, 30)
    assert tc1_on.days == 0xFE  # every day (bits 1..7)
    assert (tc1_off.hour, tc1_off.minute) == (6, 0)

    # Installer / PCAccess codes (PII; both repr=False).
    assert 0 < acct.installer_code <= 0xFFFF
    assert 0 < acct.pc_access_code <= 0xFFFF
    assert acct.enable_pc_access is True
    r = repr(acct)
    assert "installer_code" not in r
    assert "pc_access_code" not in r

    # Geographic configuration — northern-US install on Pacific time.
    assert 25 <= acct.latitude <= 49      # continental US lat range
    assert 67 <= acct.longitude <= 125    # continental US long range
    assert acct.time_zone in (5, 6, 7, 8, 9, 10)  # US zones EST..AKST

    # Telephony / dialer scalars + the panel's own number (PII).
    assert acct.telephone_access is True
    assert acct.rings_before_answer == 8
    assert acct.my_phone_number != ""        # a real number is set
    assert "my_phone_number" not in repr(acct)  # but never in repr
    assert acct.callback_number == "-"       # blank-number sentinel

    # Misc panel scalars.
    assert acct.house_code == 1              # base X10 house code A
    assert acct.num_thermostats == 64        # OMNI_PRO_II thermostat cap
    assert acct.flash_light_num == 2         # X10 unit flashed on alarm
    assert acct.verify_fire_alarms is True
    assert acct.enable_console_emg is True
    assert acct.high_security is False

    # DCM dialer block — not configured for monitoring in this fixture
    # ("-" blank phone numbers) but the per-zone alarm-code table and
    # emergency codes are still populated.
    assert acct.dcm.phone_number_1 == "-"
    assert "phone_number_1" not in repr(acct.dcm)  # PII repr=False
    assert len(acct.dcm.zone_alarm_codes) == 176
    assert len(acct.dcm.emergency_codes) == 8
    assert all(0 <= c <= 255 for c in acct.dcm.emergency_codes)

    # Codes: PINs decode as BE u16. PII fields not in repr().
    assert acct.code_authority[1] == 1   # COMPUTER → User
    assert acct.code_authority[4] == 2   # Debra → Manager
    assert acct.code_authority[5] == 3   # Cage → Installer
    assert 0 <= acct.code_pins[1] <= 0xFFFF
    assert "code_pins" not in repr(acct)
    assert state.zones[1].name == "GARAGE ENTRY"
    assert state.units[1].name == "ROOM ONE"
    assert state.thermostats[1].name == "DOWNSTAIRS"
    # Zone types from SetupData — door zones are EntryExit (0) or
    # Perimeter (1), motion sensors are AwayInt (3), the OUTSIDE TEMP
    # zone is Extended_Range_OutdoorTemp (0x55).
    assert state.zones[1].zone_type == 0x00     # GARAGE ENTRY → EntryExit
    assert state.zones[2].zone_type == 0x00     # FRONT DOOR → EntryExit
    assert state.zones[3].zone_type == 0x01     # BACK DOOR → Perimeter
    assert state.zones[7].zone_type == 0x03     # LIVINGROOM MOT → AwayInt
    assert state.zones[11].zone_type == 0x55    # OUTSIDE TEMP → outdoor temp
    # Zone area assignments from SetupData — single-area install, all
    # zones in area 1.
    for slot, zone in state.zones.items():
        assert zone.area == 1, f"slot {slot} expected area=1 got {zone.area}"
    # ZoneOptions — every zone carries the panel-default 4 in this fixture.
    for slot, zone in state.zones.items():
        assert zone.options == 4, f"slot {slot} expected options=4 got {zone.options}"
    assert all(v == 4 for v in acct.zone_options.values())
    assert len(acct.zone_options) == 176

    # Thermostat type + area from SetupData. The two named thermostats
    # (DOWNSTAIRS, UPSTAIRS) are type 1; areas were 0xFF (all) →
    # normalised to area 1 only in MockState.
    assert acct.thermostat_types[1] == 1
    assert acct.thermostat_types[2] == 1
    assert len(acct.thermostat_types) == 64
    assert state.thermostats[1].thermostat_type == 1
    assert state.thermostats[1].areas == 0x01

    # Four scalars sandwiched around the thermostat arrays.
    assert acct.time_adj == 30              # panel default
    assert 1 <= acct.alarm_reset_time <= 30  # in valid standard range
    assert acct.arming_confirmation is False
    assert acct.two_way_audio is False

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


# ---- DownloadProgram writeback ------------------------------------------


@pytest.mark.asyncio
async def test_v2_download_program_writes_slot() -> None:
    """Writing a Program via DownloadProgram lands it in MockState; a
    subsequent UploadProgram returns the same bytes — proving the
    full read-then-write-then-read loop works against the mock."""
    from omni_pca.client import OmniClient
    from omni_pca.commands import Command

    target = Program(
        slot=42, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=22, minute=30,
        days=int(Days.MONDAY | Days.WEDNESDAY | Days.FRIDAY),
    )
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0,
        ) as c:
            # Slot 42 starts empty.
            assert 42 not in panel.state.programs
            await c.download_program(42, target)
            # Now the mock's state should carry the wire bytes.
            assert 42 in panel.state.programs
            assert panel.state.programs[42] == target.encode_wire_bytes()
            # And a read-back via iter_programs should yield the same program.
            programs = [p async for p in c.iter_programs()]
    assert len(programs) == 1
    p = programs[0]
    assert p.slot == 42
    assert p.prog_type == int(ProgramType.TIMED)
    assert p.cmd == int(Command.UNIT_ON)
    assert p.pr2 == 7
    assert p.hour == 22 and p.minute == 30
    assert p.days == int(Days.MONDAY | Days.WEDNESDAY | Days.FRIDAY)


@pytest.mark.asyncio
async def test_v2_download_program_overwrites_existing_slot() -> None:
    """Writing to a slot that already has a program replaces it."""
    from omni_pca.client import OmniClient
    from omni_pca.commands import Command

    original = Program(
        slot=10, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_OFF), pr2=1,
        hour=6, minute=0, days=int(Days.MONDAY),
    )
    replacement = Program(
        slot=10, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=99,
        hour=22, minute=0, days=int(Days.SUNDAY),
    )
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={10: original.encode_wire_bytes()}),
    )
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0,
        ) as c:
            await c.download_program(10, replacement)
    assert panel.state.programs[10] == replacement.encode_wire_bytes()


@pytest.mark.asyncio
async def test_v2_clear_program_removes_slot() -> None:
    """``clear_program`` writes an all-zero body, which the mock treats
    as deletion — subsequent reads see the slot as undefined."""
    from omni_pca.client import OmniClient
    from omni_pca.commands import Command

    seed = Program(
        slot=5, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=1,
        hour=6, minute=0, days=int(Days.MONDAY),
    )
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(programs={5: seed.encode_wire_bytes()}),
    )
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0,
        ) as c:
            await c.clear_program(5)
    assert 5 not in panel.state.programs


@pytest.mark.asyncio
async def test_v2_download_program_rejects_out_of_range_slot() -> None:
    """Client-side range check catches bad slot before sending."""
    from omni_pca.client import OmniClient

    p = Program(slot=1, prog_type=int(ProgramType.TIMED))
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with panel.serve(transport="tcp") as (host, port):
        async with OmniClient(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0,
        ) as c:
            with pytest.raises(ValueError, match="out of range"):
                await c.download_program(0, p)
            with pytest.raises(ValueError, match="out of range"):
                await c.download_program(1501, p)


@pytest.mark.asyncio
async def test_v1_download_program_raises_not_implemented() -> None:
    """v1 has no single-slot write; the client raises a structured
    NotImplementedError so HA can surface the limitation."""
    from omni_pca.v1 import OmniClientV1

    p = Program(slot=1, prog_type=int(ProgramType.TIMED))
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0,
        ) as c:
            with pytest.raises(NotImplementedError, match="v1 panels"):
                await c.download_program(1, p)


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
