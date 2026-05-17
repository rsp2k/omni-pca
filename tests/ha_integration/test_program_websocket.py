"""HA websocket commands for the program viewer.

Tests run against the live HA test harness so they exercise:
  * websocket command registration during integration setup
  * filter / pagination / search of the program list
  * detail rendering for compact-form + clausal-chain slots
  * fire-program over the wire to the mock panel

Each test uses the already-seeded ``configured_panel`` fixture from
conftest.py plus the test-harness-provided ``hass_ws_client``.
"""

from __future__ import annotations

import pytest
from custom_components.omni_pca.const import DOMAIN
from homeassistant.core import HomeAssistant

from omni_pca.commands import Command
from omni_pca.programs import Days, Program, ProgramType


@pytest.fixture
def seeded_programs() -> dict[int, Program]:
    """A small set of programs covering the main shapes the viewer renders.

    Three compact-form TIMED programs and one clausal chain — enough
    to exercise summary rendering, the chain group-and-render path, and
    the filter/search dimensions.
    """
    return {
        12: Program(
            slot=12, prog_type=int(ProgramType.TIMED),
            cmd=int(Command.UNIT_ON), pr2=1,  # unit 1 in fixture
            hour=6, minute=0, days=int(Days.MONDAY | Days.FRIDAY),
        ),
        42: Program(
            slot=42, prog_type=int(ProgramType.TIMED),
            cmd=int(Command.UNIT_ON), pr2=2,  # unit 2
            hour=22, minute=30, days=int(Days.SUNDAY),
        ),
        99: Program(
            slot=99, prog_type=int(ProgramType.EVENT),
            cmd=int(Command.UNIT_ON), pr2=1,
            # WHEN zone 1 changes to NOT_READY (event_id = 0x0401)
            month=0x04, day=0x01,
        ),
        # A clausal chain spanning slots 200..203: WHEN zone 1 not-ready
        # AND IF unit 1 ON THEN turn ON unit 2 AND turn OFF unit 1.
        200: Program(
            slot=200, prog_type=int(ProgramType.WHEN),
            # event_id = 0x0401 (zone 1 not-ready) packed in month/day
            month=0x04, day=0x01,
        ),
        201: Program(
            slot=201, prog_type=int(ProgramType.AND),
            # Traditional AND: family byte 0x0A = CTRL+ON, instance 1.
            # and_family = cond & 0xFF, and_instance = (cond2>>8) & 0xFF.
            cond=0x000A, cond2=0x0100,
        ),
        202: Program(
            slot=202, prog_type=int(ProgramType.THEN),
            cmd=int(Command.UNIT_ON), pr2=2,
        ),
        203: Program(
            slot=203, prog_type=int(ProgramType.THEN),
            cmd=int(Command.UNIT_OFF), pr2=1,
        ),
    }


@pytest.fixture
def populated_state(seeded_programs):
    """Override the conftest fixture to inject our test programs."""
    from omni_pca.mock_panel import (
        MockAreaState,
        MockButtonState,
        MockState,
        MockThermostatState,
        MockUnitState,
        MockZoneState,
    )

    return MockState(
        zones={
            1: MockZoneState(name="FRONT_DOOR"),
            2: MockZoneState(name="GARAGE_ENTRY"),
            10: MockZoneState(name="LIVING_MOTION"),
        },
        units={
            1: MockUnitState(name="LIVING_LAMP"),
            2: MockUnitState(name="KITCHEN_OVERHEAD"),
        },
        areas={1: MockAreaState(name="MAIN")},
        thermostats={1: MockThermostatState(name="LIVING_ROOM")},
        buttons={1: MockButtonState(name="GOOD_MORNING")},
        user_codes={1: 1234},
        programs={
            slot: p.encode_wire_bytes() for slot, p in seeded_programs.items()
        },
    )


async def test_ws_list_programs_returns_summaries(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """The list command returns rendered summary tokens for every
    program the coordinator discovered."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": configured_panel.entry_id,
    })
    response = await client.receive_json()
    assert response["success"] is True
    result = response["result"]
    # 3 compact-form programs (12, 42, 99) + 1 clausal chain (head at
    # slot 200, spanning 200..203). The chain renders as a single row.
    assert result["total"] == 4
    assert result["filtered_total"] == 4
    rows_by_slot = {row["slot"]: row for row in result["programs"]}
    assert rows_by_slot.keys() == {12, 42, 99, 200}
    assert rows_by_slot[200]["kind"] == "chain"
    assert rows_by_slot[12]["kind"] == "compact"


async def test_ws_list_programs_filter_by_trigger_type(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """``trigger_types=["TIMED"]`` filters out the EVENT-typed row."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": configured_panel.entry_id,
        "trigger_types": ["TIMED"],
    })
    response = await client.receive_json()
    assert response["success"] is True
    result = response["result"]
    assert result["filtered_total"] == 2  # only the two TIMED rows
    assert {row["slot"] for row in result["programs"]} == {12, 42}


async def test_ws_list_programs_filter_by_referenced_entity(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """``references_entity="unit:2"`` returns only programs that mention unit 2."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": configured_panel.entry_id,
        "references_entity": "unit:2",
    })
    response = await client.receive_json()
    result = response["result"]
    # Slot 42 ("Turn ON KITCHEN_OVERHEAD" = unit 2) plus the seeded chain
    # at slot 200 (action: Turn ON unit 2) both reference unit:2.
    assert result["filtered_total"] == 2
    slots = {r["slot"] for r in result["programs"]}
    assert slots == {42, 200}


async def test_ws_list_programs_search_substring(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Search is a case-insensitive substring match on the rendered text."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": configured_panel.entry_id,
        "search": "kitchen",
    })
    response = await client.receive_json()
    result = response["result"]
    # Slot 42 ("Turn ON KITCHEN_OVERHEAD" — truncated to 12 chars on
    # wire = "KITCHEN_OVER") matches. The chain at slot 200 also has
    # an action against unit 2 which renders with the same truncated
    # name, so it matches too.
    assert result["filtered_total"] == 2
    slots = {r["slot"] for r in result["programs"]}
    assert slots == {42, 200}


async def test_ws_list_programs_pagination(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": configured_panel.entry_id,
        "limit": 2,
        "offset": 1,
    })
    response = await client.receive_json()
    result = response["result"]
    # 4 list rows total: 3 compact + 1 chain head.
    assert result["filtered_total"] == 4
    assert len(result["programs"]) == 2
    assert [row["slot"] for row in result["programs"]] == [42, 99]


async def test_ws_get_program_returns_full_token_stream(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Detail of a single slot returns the full structured-English tokens."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/get",
        "entry_id": configured_panel.entry_id,
        "slot": 42,
    })
    response = await client.receive_json()
    assert response["success"] is True
    result = response["result"]
    assert result["slot"] == 42
    assert result["kind"] == "compact"
    assert result["trigger_type"] == "TIMED"
    text = "".join(
        tok["t"] for tok in result["tokens"] if tok.get("k") != "newline"
    )
    assert "Turn ON" in text
    # Unit names cap at 12 bytes on Omni Pro II (lenUnitName), so the
    # 16-char "KITCHEN_OVERHEAD" lands on the wire as "KITCHEN_OVER".
    assert "KITCHEN_OVER" in text


async def test_ws_get_program_returns_raw_fields_for_editor(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """The detail response includes a 'fields' dict carrying raw Program
    integer values, so the editor can seed forms from actual data rather
    than defaults. Round-trip: get → fields → write back should preserve
    every byte (idempotent under no-op edits)."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/get",
        "entry_id": configured_panel.entry_id,
        "slot": 42,
    })
    response = await client.receive_json()
    assert response["success"] is True
    fields = response["result"]["fields"]
    # Slot 42 is the seeded TIMED 22:30 Sunday → Turn ON unit 2 program.
    assert fields["prog_type"] == 1
    assert fields["hour"] == 22
    assert fields["minute"] == 30
    assert fields["days"] == int(Days.SUNDAY)
    assert fields["cmd"] == int(Command.UNIT_ON)
    assert fields["pr2"] == 2

    # Round-trip: write those same fields back; nothing should change.
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    before = coordinator.data.programs[42]
    await client.send_json_auto_id({
        "type": "omni_pca/programs/write",
        "entry_id": configured_panel.entry_id,
        "slot": 42,
        "program": fields,
    })
    write_response = await client.receive_json()
    assert write_response["success"] is True
    after = coordinator.data.programs[42]
    assert before.encode_wire_bytes() == after.encode_wire_bytes()


async def test_ws_get_program_missing_slot_returns_error(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/get",
        "entry_id": configured_panel.entry_id,
        "slot": 500,  # not seeded
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_ws_list_programs_unknown_entry_id(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Bad entry_id returns a structured ``not_found`` error, not a crash."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": "does-not-exist",
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_ws_fire_program_executes_command(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Fire sends Command.EXECUTE_PROGRAM over the wire — the mock acks."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/fire",
        "entry_id": configured_panel.entry_id,
        "slot": 42,
    })
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"] == {"slot": 42, "fired": True}


async def test_ws_clear_program_writes_zero_body(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Clear erases a slot end-to-end: ws command → DownloadProgram on
    the wire → mock state loses the slot → coordinator drops it from
    its in-memory map."""
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    assert 42 in coordinator.data.programs
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/clear",
        "entry_id": configured_panel.entry_id,
        "slot": 42,
    })
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"] == {"slot": 42, "cleared": True}
    # The coordinator's view drops the slot immediately so a follow-up
    # list reflects the deletion without waiting for the next poll.
    assert 42 not in coordinator.data.programs


async def test_ws_clone_program_copies_to_empty_slot(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Cloning slot 12 to slot 500 lands a copy at the target with the
    right fields and leaves the source untouched."""
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    assert 12 in coordinator.data.programs
    assert 500 not in coordinator.data.programs
    source = coordinator.data.programs[12]
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/clone",
        "entry_id": configured_panel.entry_id,
        "source_slot": 12,
        "target_slot": 500,
    })
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"] == {
        "source_slot": 12, "target_slot": 500, "cloned": True,
    }
    # New program landed at the target with re-stamped slot.
    cloned = coordinator.data.programs[500]
    assert cloned.slot == 500
    assert cloned.prog_type == source.prog_type
    assert cloned.cmd == source.cmd
    assert cloned.pr2 == source.pr2
    # Source remains.
    assert 12 in coordinator.data.programs


async def test_ws_clone_program_rejects_same_slot(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/clone",
        "entry_id": configured_panel.entry_id,
        "source_slot": 12,
        "target_slot": 12,
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid"


async def test_ws_clone_program_rejects_missing_source(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Cloning from a slot that has no program is a structured error."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/clone",
        "entry_id": configured_panel.entry_id,
        "source_slot": 999,  # not seeded
        "target_slot": 100,
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "not_found"


async def test_ws_write_program_creates_new_slot(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Writing a Program dict to an empty slot lands a new program."""
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    assert 700 not in coordinator.data.programs
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/write",
        "entry_id": configured_panel.entry_id,
        "slot": 700,
        "program": {
            "prog_type": 1,        # TIMED
            "cmd": int(Command.UNIT_ON),
            "pr2": 2,
            "hour": 7, "minute": 30,
            "days": int(Days.SATURDAY | Days.SUNDAY),
        },
    })
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"] == {"slot": 700, "written": True}
    new_program = coordinator.data.programs[700]
    assert new_program.slot == 700
    assert new_program.cmd == int(Command.UNIT_ON)
    assert new_program.pr2 == 2
    assert new_program.hour == 7 and new_program.minute == 30


async def test_ws_write_program_overwrites_existing_slot(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Writing to a slot that has a program replaces the existing one."""
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    # Slot 12 is seeded (TIMED hour=6 minute=0). Rewrite it.
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/write",
        "entry_id": configured_panel.entry_id,
        "slot": 12,
        "program": {
            "prog_type": 1,
            "cmd": int(Command.UNIT_OFF),
            "pr2": 99,
            "hour": 23, "minute": 45, "days": int(Days.MONDAY),
        },
    })
    response = await client.receive_json()
    assert response["success"] is True
    updated = coordinator.data.programs[12]
    assert updated.cmd == int(Command.UNIT_OFF)
    assert updated.pr2 == 99
    assert updated.hour == 23 and updated.minute == 45


async def test_ws_write_program_validates_payload(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Bad program dict (out-of-range field) returns structured error."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/write",
        "entry_id": configured_panel.entry_id,
        "slot": 12,
        "program": {
            "prog_type": 99,  # invalid (max 10)
            "cmd": 1, "pr2": 1, "hour": 6, "minute": 0,
        },
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid"


async def test_ws_list_objects_returns_named_buckets(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """objects/list returns zones/units/areas/thermostats/buttons in
    slot-sorted order with their HA-discovered names."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/objects/list",
        "entry_id": configured_panel.entry_id,
    })
    response = await client.receive_json()
    assert response["success"] is True
    result = response["result"]
    assert {"zones", "units", "areas", "thermostats", "buttons"} <= result.keys()
    # Fixture has units at indexes 1, 2 (LIVING_LAMP, KITCHEN_OVERHEAD-truncated).
    units = result["units"]
    assert len(units) == 2
    assert units[0]["index"] == 1
    assert units[0]["name"] == "LIVING_LAMP"
    # And zones come back with their fixture names too.
    zones_by_idx = {z["index"]: z["name"] for z in result["zones"]}
    assert zones_by_idx[1] == "FRONT_DOOR"


async def test_ws_get_chain_returns_member_fields(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Chain detail response includes a chain_members array with each
    member's role + raw fields, so the editor can render an editable
    row per slot."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/get",
        "entry_id": configured_panel.entry_id,
        "slot": 200,  # head of the seeded chain
    })
    response = await client.receive_json()
    assert response["success"] is True
    result = response["result"]
    assert result["kind"] == "chain"
    members = result["chain_members"]
    roles = [m["role"] for m in members]
    assert roles == ["head", "condition", "action", "action"]
    # Head carries the event_id (zone 1 NOT_READY = 0x0401).
    head_fields = members[0]["fields"]
    assert head_fields["prog_type"] == int(ProgramType.WHEN)
    assert head_fields["month"] == 0x04
    assert head_fields["day"] == 0x01
    # Condition is a Traditional AND record with family CTRL+ON, unit 1.
    cond_fields = members[1]["fields"]
    assert cond_fields["prog_type"] == int(ProgramType.AND)
    assert cond_fields["cond"] == 0x000A
    assert cond_fields["cond2"] == 0x0100


async def test_ws_chain_write_replaces_in_place(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Same-length rewrite leaves the chain footprint unchanged but
    updates every member's bytes."""
    client = await hass_ws_client(hass)
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    # Existing chain: slots 200..203.
    assert {200, 201, 202, 203} <= coordinator.data.programs.keys()
    await client.send_json_auto_id({
        "type": "omni_pca/programs/chain/write",
        "entry_id": configured_panel.entry_id,
        "head_slot": 200,
        "head": {
            "prog_type": int(ProgramType.WHEN),
            "month": 0x04, "day": 0x02,        # zone 1 trouble (id 0x0402)
        },
        "conditions": [
            # AND IF unit 2 ON (family 0x0A, instance 2)
            {"prog_type": int(ProgramType.AND),
             "cond": 0x000A, "cond2": 0x0200},
        ],
        "actions": [
            {"prog_type": int(ProgramType.THEN),
             "cmd": int(Command.UNIT_OFF), "pr2": 2},
            {"prog_type": int(ProgramType.THEN),
             "cmd": int(Command.UNIT_ON), "pr2": 1},
        ],
    })
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["written_slots"] == [200, 201, 202, 203]
    assert response["result"]["cleared_slots"] == []
    # Coordinator state reflects the new bytes.
    assert coordinator.data.programs[200].day == 0x02
    assert coordinator.data.programs[201].cond2 == 0x0200
    assert coordinator.data.programs[202].cmd == int(Command.UNIT_OFF)


async def test_ws_chain_write_shrinks_and_clears(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Shorter rewrite clears the trailing old chain slots."""
    client = await hass_ws_client(hass)
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    await client.send_json_auto_id({
        "type": "omni_pca/programs/chain/write",
        "entry_id": configured_panel.entry_id,
        "head_slot": 200,
        "head": {
            "prog_type": int(ProgramType.WHEN),
            "month": 0x04, "day": 0x01,
        },
        # No conditions, one action — chain shrinks from 4 slots to 2.
        "conditions": [],
        "actions": [
            {"prog_type": int(ProgramType.THEN),
             "cmd": int(Command.UNIT_ON), "pr2": 1},
        ],
    })
    response = await client.receive_json()
    assert response["success"] is True
    assert response["result"]["written_slots"] == [200, 201]
    assert sorted(response["result"]["cleared_slots"]) == [202, 203]
    # Cleared slots are gone from the coordinator's view.
    assert 202 not in coordinator.data.programs
    assert 203 not in coordinator.data.programs


async def test_ws_chain_write_refuses_to_trample(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Expanding a chain into a slot that already holds another program
    is refused — protects against accidental data loss."""
    client = await hass_ws_client(hass)
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    # Seed a sentinel program at slot 204 (right after the chain) so an
    # expand attempt collides.
    coordinator.data.programs[204] = Program(
        slot=204, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=1,
        hour=12, minute=0, days=int(Days.MONDAY),
    )
    await client.send_json_auto_id({
        "type": "omni_pca/programs/chain/write",
        "entry_id": configured_panel.entry_id,
        "head_slot": 200,
        "head": {"prog_type": int(ProgramType.WHEN),
                 "month": 0x04, "day": 0x01},
        "conditions": [
            {"prog_type": int(ProgramType.AND),
             "cond": 0x000A, "cond2": 0x0100},
            # Adding a second condition pushes the chain from 4 to 5
            # slots → slot 204 collision.
            {"prog_type": int(ProgramType.AND),
             "cond": 0x000A, "cond2": 0x0200},
        ],
        "actions": [
            {"prog_type": int(ProgramType.THEN),
             "cmd": int(Command.UNIT_ON), "pr2": 2},
            {"prog_type": int(ProgramType.THEN),
             "cmd": int(Command.UNIT_OFF), "pr2": 1},
        ],
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid"
    # The sentinel program is untouched.
    assert coordinator.data.programs[204].cmd == int(Command.UNIT_ON)


async def test_ws_chain_write_rejects_zero_actions(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """A chain with no THEN actions is meaningless — refuse it."""
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/chain/write",
        "entry_id": configured_panel.entry_id,
        "head_slot": 200,
        "head": {"prog_type": int(ProgramType.WHEN),
                 "month": 0x04, "day": 0x01},
        "conditions": [],
        "actions": [],
    })
    response = await client.receive_json()
    assert response["success"] is False
    assert response["error"]["code"] == "invalid"


async def test_ws_list_programs_live_state_overlay_zone(
    hass: HomeAssistant, configured_panel, hass_ws_client
) -> None:
    """Summary tokens carry live-state badges on REF tokens.

    The EVENT program at slot 99 references zone 1; the coordinator's
    ``zone_status[1]`` carries SECURE / NOT READY etc. and we expect
    that label to flow through to the token's ``s`` field.
    """
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({
        "type": "omni_pca/programs/list",
        "entry_id": configured_panel.entry_id,
    })
    response = await client.receive_json()
    rows_by_slot = {row["slot"]: row for row in response["result"]["programs"]}
    event_row = rows_by_slot[99]
    refs = [tok for tok in event_row["summary"] if tok.get("k") == "ref"]
    # At least one REF should be the zone-1 reference with a state label.
    zone_refs = [r for r in refs if r.get("ek") == "zone" and r.get("ei") == 1]
    assert zone_refs, f"expected zone:1 ref in {refs!r}"
    assert "s" in zone_refs[0]  # state badge populated
