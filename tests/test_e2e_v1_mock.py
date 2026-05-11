"""End-to-end: OmniClientV1 ↔ MockPanel speaking the v1 wire dialect.

Exercises the MockPanel's new ``_dispatch_v1`` path over UDP (which
is what ``OmniClientV1`` opens — see :class:`omni_pca.v1.connection.
OmniConnectionV1`). The packets travel ``127.0.0.1`` so there is no
real packet-loss risk; we still set a 2 s per-reply timeout to fail
fast if the dispatcher hangs.
"""

from __future__ import annotations

import pytest

from omni_pca.commands import CommandFailedError
from omni_pca.mock_panel import (
    MockAreaState,
    MockButtonState,
    MockPanel,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)
from omni_pca.models import SecurityMode
from omni_pca.v1 import NameType, OmniClientV1

CONTROLLER_KEY = bytes.fromhex("6ba7b4e9b4656de3cd7edd4c650cdb09")


def _populated_state() -> MockState:
    return MockState(
        zones={
            1: MockZoneState(name="FRONT DOOR"),
            2: MockZoneState(name="BACK DOOR"),
            3: MockZoneState(name="LIVING MOT", current_state=1, loop=0xFD),
        },
        units={
            1: MockUnitState(name="FRONT PORCH", state=1),       # on
            2: MockUnitState(name="LIVING LAMP", state=0x96),    # 50% brightness
        },
        areas={1: MockAreaState(name="MAIN", mode=int(SecurityMode.OFF))},
        thermostats={
            1: MockThermostatState(
                name="DOWNSTAIRS",
                temperature_raw=170, heat_setpoint_raw=140,
                cool_setpoint_raw=200, system_mode=1, fan_mode=0, hold_mode=0,
            ),
        },
        buttons={1: MockButtonState(name="GOOD MORNING")},
        user_codes={1: 1234},
    )


# ---- handshake + read API ------------------------------------------------


@pytest.mark.asyncio
async def test_v1_handshake_and_system_information() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            info = await c.get_system_information()
            assert info.model_name == "Omni Pro II"
            assert info.firmware_version == "2.12r1"


@pytest.mark.asyncio
async def test_v1_get_system_status_reports_areas() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            status = await c.get_system_status()
            # Mock emits 8 area mode bytes (Omni Pro II cap).
            assert len(status.area_alarms) == 8
            # Each tuple is (mode, 0); area 1 was OFF (0).
            assert status.area_alarms[0] == (0, 0)


@pytest.mark.asyncio
async def test_v1_zone_status_short_form() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            zones = await c.get_zone_status(1, 8)
            assert len(zones) == 8
            assert zones[1].is_secure
            # Zone 3 has current_state=1 (NotReady -> open).
            assert zones[3].is_open
            assert zones[3].loop == 0xFD


@pytest.mark.asyncio
async def test_v1_unit_status_short_form() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            units = await c.get_unit_status(1, 4)
            assert units[1].is_on
            assert units[2].brightness == 50    # state=0x96 == 150 -> 50%
            assert not units[3].is_on            # undefined slot, defaults
            assert not units[4].is_on


@pytest.mark.asyncio
async def test_v1_unit_status_long_form() -> None:
    """Force the BE-u16 wire form by including indices > 255."""
    state = _populated_state()
    state.units[300] = MockUnitState(name="SPRINKLER-Z3", state=1)
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            units = await c.get_unit_status(298, 302)
            assert len(units) == 5
            assert units[300].is_on


@pytest.mark.asyncio
async def test_v1_thermostat_status() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            tstats = await c.get_thermostat_status(1, 1)
            t = tstats[1]
            assert t.temperature_raw == 170
            assert t.heat_setpoint_raw == 140
            assert t.cool_setpoint_raw == 200
            assert t.system_mode == 1
            assert t.fan_mode == 0
            assert t.hold_mode == 0


# ---- UploadNames streaming ----------------------------------------------


@pytest.mark.asyncio
async def test_v1_upload_names_streams_all_objects() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            all_names = await c.list_all_names()

    # Expected: Zones 1-3, Units 1-2, Button 1, Area 1, Thermostat 1.
    assert set(all_names.keys()) == {
        int(NameType.ZONE),
        int(NameType.UNIT),
        int(NameType.BUTTON),
        int(NameType.AREA),
        int(NameType.THERMOSTAT),
    }
    assert all_names[int(NameType.ZONE)] == {
        1: "FRONT DOOR", 2: "BACK DOOR", 3: "LIVING MOT",
    }
    assert all_names[int(NameType.UNIT)] == {
        1: "FRONT PORCH", 2: "LIVING LAMP",
    }
    assert all_names[int(NameType.BUTTON)] == {1: "GOOD MORNING"}
    assert all_names[int(NameType.AREA)] == {1: "MAIN"}
    assert all_names[int(NameType.THERMOSTAT)] == {1: "DOWNSTAIRS"}


@pytest.mark.asyncio
async def test_v1_upload_names_empty_panel_returns_no_records() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY)
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            all_names = await c.list_all_names()
    assert all_names == {}


@pytest.mark.asyncio
async def test_v1_upload_names_two_byte_form_for_high_indices() -> None:
    state = _populated_state()
    state.units[300] = MockUnitState(name="Z-LANDSCAPE")  # > 255
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            all_names = await c.list_all_names()
    assert all_names[int(NameType.UNIT)][300] == "Z-LANDSCAPE"


# ---- write methods ------------------------------------------------------


@pytest.mark.asyncio
async def test_v1_turn_unit_on_mutates_mock_state() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            assert panel.state.units[2].state == 0x96  # 50%
            await c.set_unit_level(2, 75)
            assert panel.state.units[2].state == 100 + 75  # 175 = 75%


@pytest.mark.asyncio
async def test_v1_bypass_and_restore_zone() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            await c.bypass_zone(1, code=1)
            assert panel.state.zones[1].is_bypassed
            await c.restore_zone(1, code=1)
            assert not panel.state.zones[1].is_bypassed


@pytest.mark.asyncio
async def test_v1_execute_security_command_arm_away() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            await c.execute_security_command(
                area=1, mode=SecurityMode.AWAY, code=1234
            )
    assert panel.state.areas[1].mode == int(SecurityMode.AWAY)


@pytest.mark.asyncio
async def test_v1_execute_security_command_wrong_code() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        async with OmniClientV1(
            host=host, port=port, controller_key=CONTROLLER_KEY, timeout=2.0
        ) as c:
            with pytest.raises(CommandFailedError):
                await c.execute_security_command(
                    area=1, mode=SecurityMode.AWAY, code=9999
                )
    # State unchanged after failed command.
    assert panel.state.areas[1].mode == int(SecurityMode.OFF)
