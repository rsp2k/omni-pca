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
    assert result["total"] == 3
    assert result["filtered_total"] == 3
    rows_by_slot = {row["slot"]: row for row in result["programs"]}
    # Both TIMED programs and the EVENT program land in the response.
    assert rows_by_slot.keys() == {12, 42, 99}
    # Each row has the metadata the frontend needs.
    for row in result["programs"]:
        assert row["kind"] == "compact"
        assert row["trigger_type"] in ("TIMED", "EVENT")
        assert isinstance(row["summary"], list)
        assert row["summary"]  # non-empty token list


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
    assert result["filtered_total"] == 1
    assert result["programs"][0]["slot"] == 42


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
    # Only slot 42 ("Turn ON KITCHEN_OVERHEAD") mentions kitchen.
    assert result["filtered_total"] == 1
    assert result["programs"][0]["slot"] == 42


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
    assert result["filtered_total"] == 3
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
