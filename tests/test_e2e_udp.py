"""End-to-end: OmniClient ↔ MockPanel over UDP.

Mirrors test_e2e_client_mock.py but with ``transport='udp'`` on both
sides. The protocol/encryption/handshake bytes are identical to TCP;
this proves only the transport layer change is sound.
"""

from __future__ import annotations

import asyncio
import secrets

import pytest

from omni_pca.client import ObjectType, OmniClient
from omni_pca.commands import CommandFailedError
from omni_pca.connection import ConnectionState, HandshakeError, OmniConnection
from omni_pca.events import UnitStateChanged
from omni_pca.mock_panel import (
    MockAreaState,
    MockButtonState,
    MockPanel,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)
from omni_pca.models import (
    AreaStatus,
    SecurityMode,
)
from omni_pca.opcodes import OmniLink2MessageType

CONTROLLER_KEY = bytes.fromhex("6ba7b4e9b4656de3cd7edd4c650cdb09")


def _populated_state() -> MockState:
    return MockState(
        zones={1: MockZoneState(name="FRONT_DOOR")},
        units={1: MockUnitState(name="LIVING_LAMP")},
        areas={1: MockAreaState(name="MAIN")},
        thermostats={1: MockThermostatState(name="LIVING")},
        buttons={1: MockButtonState(name="GOOD_MORNING")},
        user_codes={1: 1234},
    )


async def test_udp_handshake_roundtrip() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with (
        panel.serve(transport="udp") as (host, port),
        OmniConnection(
            host=host,
            port=port,
            controller_key=CONTROLLER_KEY,
            transport="udp",
            timeout=2.0,
        ) as conn,
    ):
        assert conn.state is ConnectionState.ONLINE
    assert panel.session_count == 1


async def test_udp_get_system_information() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with (
        panel.serve(transport="udp") as (host, port),
        OmniConnection(
            host=host,
            port=port,
            controller_key=CONTROLLER_KEY,
            transport="udp",
            timeout=2.0,
        ) as conn,
    ):
        reply = await conn.request(OmniLink2MessageType.RequestSystemInformation)
        assert reply.opcode == int(OmniLink2MessageType.SystemInformation)
        # First payload byte is the model byte.
        assert reply.payload[0] == 16  # OMNI_PRO_II


async def test_udp_arm_area_with_correct_code() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with (
        panel.serve(transport="udp") as (host, port),
        OmniClient(
            host=host,
            port=port,
            controller_key=CONTROLLER_KEY,
            transport="udp",
            timeout=2.0,
        ) as client,
    ):
        await client.execute_security_command(
            area=1, mode=SecurityMode.AWAY, code=1234,
        )
        statuses = await client.get_object_status(ObjectType.AREA, 1)
    assert len(statuses) == 1
    area = statuses[0]
    assert isinstance(area, AreaStatus)
    assert area.mode == int(SecurityMode.AWAY)


async def test_udp_arm_with_wrong_code_raises() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with panel.serve(transport="udp") as (host, port):
        with pytest.raises(CommandFailedError):
            async with OmniClient(
                host=host,
                port=port,
                controller_key=CONTROLLER_KEY,
                transport="udp",
                timeout=2.0,
            ) as client:
                await client.execute_security_command(
                    area=1, mode=SecurityMode.AWAY, code=9999,
                )


async def test_udp_unit_on_pushes_state_changed_event() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=_populated_state())
    async with (
        panel.serve(transport="udp") as (host, port),
        OmniClient(
            host=host,
            port=port,
            controller_key=CONTROLLER_KEY,
            transport="udp",
            timeout=2.0,
        ) as client,
    ):
        events = client.events()
        await client.turn_unit_on(1)
        ev = await asyncio.wait_for(events.__anext__(), timeout=1.0)
        assert isinstance(ev, UnitStateChanged)
        assert ev.unit_index == 1
        assert ev.is_on is True


async def test_udp_wrong_key_fails_handshake() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY)
    wrong_key = secrets.token_bytes(16)
    async with panel.serve(transport="udp") as (host, port):
        with pytest.raises(HandshakeError):
            async with OmniConnection(
                host=host,
                port=port,
                controller_key=wrong_key,
                transport="udp",
                timeout=2.0,
            ):
                pass
