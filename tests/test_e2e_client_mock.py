"""End-to-end: OmniClient drives a real MockPanel over a real TCP socket.

This is the integration smoke test that proves the protocol stack actually
roundtrips. Both sides built independently — if framing, sequence numbers,
session-key derivation, or per-block whitening disagree, the handshake fails.
"""

from __future__ import annotations

import secrets

import pytest

from omni_pca.client import ObjectType, OmniClient
from omni_pca.connection import HandshakeError
from omni_pca.mock_panel import MockPanel, MockState
from omni_pca.models import AreaProperties, UnitProperties, ZoneProperties

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
