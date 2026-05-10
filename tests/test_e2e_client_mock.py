"""End-to-end: OmniClient drives a real MockPanel over a real TCP socket.

This is the integration smoke test that proves the protocol stack actually
roundtrips. Both sides built independently — if framing, sequence numbers,
session-key derivation, or per-block whitening disagree, the handshake fails.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

from omni_pca.client import ObjectType, OmniClient
from omni_pca.commands import CommandFailedError
from omni_pca.connection import HandshakeError
from omni_pca.events import ArmingChanged, UnitStateChanged
from omni_pca.mock_panel import (
    MockAreaState,
    MockPanel,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)
from omni_pca.models import (
    AreaProperties,
    AreaStatus,
    SecurityMode,
    ThermostatStatus,
    UnitProperties,
    UnitStatus,
    ZoneProperties,
    ZoneStatus,
)
from omni_pca.models import (
    ObjectType as ModelObjectType,
)

CONTROLLER_KEY = bytes.fromhex("6ba7b4e9b4656de3cd7edd4c650cdb09")


@pytest.fixture
def seeded_state() -> MockState:
    return MockState(
        model_byte=16,
        firmware_major=2,
        firmware_minor=12,
        firmware_revision=1,
        zones={1: "FRONT DOOR", 2: "GARAGE ENTRY", 7: "MASTER BED MOT"},
        units={1: "FRONT PORCH", 2: "STAIRS"},
        areas={1: "Main", 2: "Guest"},
    )


async def test_e2e_handshake_then_system_information(seeded_state: MockState) -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=seeded_state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        info = await cli.get_system_information()
        assert info.model_byte == 16
        assert info.model_name == "Omni Pro II"
        assert info.firmware_version.startswith("2.12")
    assert panel.session_count == 1


async def test_e2e_get_zone_properties(seeded_state: MockState) -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=seeded_state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        zone = await cli.get_object_properties(ObjectType.ZONE, 1)
        assert isinstance(zone, ZoneProperties)
        assert zone.index == 1
        assert zone.name == "FRONT DOOR"


async def test_e2e_get_unit_properties(seeded_state: MockState) -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=seeded_state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        unit = await cli.get_object_properties(ObjectType.UNIT, 2)
        assert isinstance(unit, UnitProperties)
        assert unit.index == 2
        assert unit.name == "STAIRS"


async def test_e2e_get_area_properties(seeded_state: MockState) -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=seeded_state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        area = await cli.get_object_properties(ObjectType.AREA, 1)
        assert isinstance(area, AreaProperties)
        assert area.index == 1
        assert area.name == "Main"


async def test_e2e_list_zone_names(seeded_state: MockState) -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=seeded_state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        names = await cli.list_zone_names()
        assert names == {1: "FRONT DOOR", 2: "GARAGE ENTRY", 7: "MASTER BED MOT"}


async def test_e2e_wrong_key_fails_with_handshake_error() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY)
    wrong_key = secrets.token_bytes(16)
    async with panel.serve() as (host, port):
        # pytest.raises is sync; can't combine into the async with above.
        with pytest.raises(HandshakeError):
            async with OmniClient(host=host, port=port, controller_key=wrong_key) as cli:
                await cli.get_system_information()


# --------------------------------------------------------------------------
# New surface: typed commands + status + event push
# --------------------------------------------------------------------------


def _state_with_area_and_codes() -> MockState:
    """Common fixture: one area with one valid user-code mapping."""
    return MockState(
        areas={1: MockAreaState(name="Main")},
        user_codes={1: 1234},
    )


async def test_e2e_arm_area() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_state_with_area_and_codes())
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        await cli.execute_security_command(
            area=1, mode=SecurityMode.AWAY, code=1234
        )
        statuses = await cli.get_object_status(ModelObjectType.AREA, 1)
        assert len(statuses) == 1
        area = statuses[0]
        assert isinstance(area, AreaStatus)
        assert area.index == 1
        assert area.mode == int(SecurityMode.AWAY)
        assert area.mode_name == "AWAY"


async def test_e2e_arm_with_wrong_code_raises() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_state_with_area_and_codes())
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        with pytest.raises(CommandFailedError):
            await cli.execute_security_command(
                area=1, mode=SecurityMode.AWAY, code=9999
            )


async def test_e2e_turn_unit_on_off() -> None:
    state = MockState(units={1: MockUnitState(name="Lamp")})
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        await cli.turn_unit_on(1)
        statuses = await cli.get_object_status(ModelObjectType.UNIT, 1)
        assert len(statuses) == 1
        unit = statuses[0]
        assert isinstance(unit, UnitStatus)
        assert unit.state == 1
        assert unit.is_on is True

        await cli.turn_unit_off(1)
        statuses = await cli.get_object_status(ModelObjectType.UNIT, 1)
        assert statuses[0].state == 0
        assert statuses[0].is_on is False


async def test_e2e_set_unit_level() -> None:
    state = MockState(units={1: MockUnitState(name="Dimmer")})
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        await cli.set_unit_level(1, 60)
        statuses = await cli.get_extended_status(ModelObjectType.UNIT, 1)
        assert len(statuses) == 1
        unit = statuses[0]
        assert isinstance(unit, UnitStatus)
        # state byte 100..200 encodes brightness percent (state - 100).
        assert unit.state == 160
        assert unit.brightness == 60


async def test_e2e_bypass_restore_zone() -> None:
    state = MockState(zones={1: MockZoneState(name="Front Door")})
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        # Initially not bypassed.
        statuses = await cli.get_object_status(ModelObjectType.ZONE, 1)
        assert isinstance(statuses[0], ZoneStatus)
        assert statuses[0].is_bypassed is False

        await cli.bypass_zone(1)
        statuses = await cli.get_object_status(ModelObjectType.ZONE, 1)
        assert statuses[0].is_bypassed is True

        await cli.restore_zone(1)
        statuses = await cli.get_object_status(ModelObjectType.ZONE, 1)
        assert statuses[0].is_bypassed is False


async def test_e2e_set_thermostat_heat_setpoint() -> None:
    state = MockState(thermostats={1: MockThermostatState(name="Living")})
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        await cli.set_thermostat_heat_setpoint_raw(1, 150)
        statuses = await cli.get_extended_status(ModelObjectType.THERMOSTAT, 1)
        assert len(statuses) == 1
        tstat = statuses[0]
        assert isinstance(tstat, ThermostatStatus)
        assert tstat.heat_setpoint_raw == 150


async def test_e2e_arm_pushes_arming_changed_event() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_state_with_area_and_codes())
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        events = cli.events()
        await cli.execute_security_command(
            area=1, mode=SecurityMode.AWAY, code=1234
        )
        ev = await asyncio.wait_for(events.__anext__(), timeout=1.0)
        assert isinstance(ev, ArmingChanged)
        assert ev.area_index == 1
        assert ev.new_mode == int(SecurityMode.AWAY)
        assert ev.user_index == 1


async def test_e2e_unit_command_pushes_unit_state_changed_event() -> None:
    state = MockState(units={1: MockUnitState(name="Lamp")})
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=state)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        events = cli.events()
        await cli.turn_unit_on(1)
        ev = await asyncio.wait_for(events.__anext__(), timeout=1.0)
        assert isinstance(ev, UnitStateChanged)
        assert ev.unit_index == 1
        assert ev.is_on is True


async def test_e2e_acknowledge_alerts() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY)
    async with (
        panel.serve() as (host, port),
        OmniClient(host=host, port=port, controller_key=CONTROLLER_KEY) as cli,
    ):
        # Should complete without raising.
        await cli.acknowledge_alerts()
