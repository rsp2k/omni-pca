"""Fixtures for the HA-side integration tests.

Each test gets:
* a fresh ``MockPanel`` listening on a random localhost port,
* a HA config entry whose ``host``/``port``/``controller_key`` point at it,
* a fully booted HA instance with the integration loaded.

The HA harness blocks real sockets by default; we re-enable them here
so the in-process client can talk to the in-process mock.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from custom_components.omni_pca.const import CONF_CONTROLLER_KEY, DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from omni_pca.mock_panel import (
    MockAreaState,
    MockButtonState,
    MockPanel,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)

CONTROLLER_KEY = bytes(range(16))
CONTROLLER_KEY_HEX = CONTROLLER_KEY.hex()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Tell HA to load components from ``custom_components/`` for every test."""
    return None


@pytest.fixture(autouse=True)
def expected_lingering_tasks() -> bool:
    """Allow the coordinator's background event-listener task to outlive the
    test body — the integration cancels it on entry unload, but the harness's
    default ``verify_cleanup`` flags any task still alive at teardown."""
    return True


@pytest.fixture(autouse=True)
def _short_scan_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cut the 30s polling interval down so tests don't wait on it."""
    from datetime import timedelta

    from custom_components.omni_pca import const, coordinator

    fast = timedelta(seconds=1)
    monkeypatch.setattr(const, "SCAN_INTERVAL", fast)
    monkeypatch.setattr(coordinator, "SCAN_INTERVAL", fast)


@pytest.fixture
def populated_state() -> MockState:
    """A lightly-populated mock state covering every entity platform."""
    from omni_pca.programs import Days, Program, ProgramType
    programs = {
        slot: prog.encode_wire_bytes()
        for slot, prog in {
            12: Program(
                slot=12, prog_type=int(ProgramType.TIMED),
                cmd=3, hour=6, minute=0,
                days=int(Days.MONDAY | Days.FRIDAY),
            ),
            42: Program(
                slot=42, prog_type=int(ProgramType.TIMED),
                cmd=4, hour=22, minute=30,
                days=int(Days.SUNDAY),
            ),
            99: Program(
                slot=99, prog_type=int(ProgramType.EVENT),
                cmd=5, month=5, day=12,
            ),
        }.items()
    }
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
        areas={
            1: MockAreaState(name="MAIN"),
        },
        thermostats={
            1: MockThermostatState(name="LIVING_ROOM"),
        },
        buttons={
            1: MockButtonState(name="GOOD_MORNING"),
        },
        user_codes={1: 1234},
        programs=programs,
    )


@pytest.fixture
async def panel(populated_state: MockState) -> AsyncIterator[tuple[MockPanel, str, int]]:
    """Spin up a MockPanel on a random localhost port for the test's lifetime."""
    mock = MockPanel(controller_key=CONTROLLER_KEY, state=populated_state)
    async with mock.serve(host="127.0.0.1") as (host, port):
        yield mock, host, port


@pytest.fixture
def config_entry_data(panel: tuple[MockPanel, str, int]) -> dict[str, Any]:
    _, host, port = panel
    return {
        CONF_HOST: host,
        CONF_PORT: port,
        CONF_CONTROLLER_KEY: CONTROLLER_KEY_HEX,
    }


@pytest.fixture
async def configured_panel(
    hass: HomeAssistant, config_entry_data: dict[str, Any]
) -> AsyncIterator[ConfigEntry]:
    """Add a config entry to HA, trigger setup, unload at teardown.

    The unload step is important — it cancels the coordinator's background
    event-listener task and closes the OmniClient socket. Without it, the
    HA harness's ``verify_cleanup`` hangs waiting for the lingering reader
    coroutine.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=config_entry_data,
        title=f"Mock Omni at {config_entry_data[CONF_HOST]}:{config_entry_data[CONF_PORT]}",
        unique_id=f"{config_entry_data[CONF_HOST]}:{config_entry_data[CONF_PORT]}",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    try:
        yield entry
    finally:
        if entry.entry_id in hass.data.get(DOMAIN, {}):
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
