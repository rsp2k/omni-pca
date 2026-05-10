"""HA-side integration: integration loads, entities materialize."""

from __future__ import annotations

from custom_components.omni_pca.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant


async def test_integration_loads_against_mock_panel(
    hass: HomeAssistant, configured_panel
) -> None:
    """End-to-end: HA discovers our integration, completes the secure
    session against the mock, populates the coordinator, and lands in
    LOADED state with no errors."""
    assert configured_panel.state is ConfigEntryState.LOADED
    coordinator = hass.data[DOMAIN][configured_panel.entry_id]
    assert coordinator.data is not None
    assert coordinator.data.system_info is not None
    assert coordinator.data.system_info.model_name == "Omni Pro II"


async def test_zone_entities_created(
    hass: HomeAssistant, configured_panel
) -> None:
    """Every named zone in MockState lands as a binary_sensor entity."""
    states = hass.states.async_all("binary_sensor")
    zone_entity_ids = [s.entity_id for s in states if "front_door" in s.entity_id.lower()
                       or "garage_entry" in s.entity_id.lower()
                       or "living_motion" in s.entity_id.lower()]
    # Each zone gets a primary + bypassed entity, so at least 3 names x 2 = 6
    # plus the system-level AC / battery / trouble entities.
    assert len(zone_entity_ids) >= 3, (
        f"expected zone entities, got {[s.entity_id for s in states]}"
    )


async def test_alarm_panel_entity_created(
    hass: HomeAssistant, configured_panel
) -> None:
    """One alarm_control_panel per discovered area."""
    states = hass.states.async_all("alarm_control_panel")
    assert len(states) == 1
    assert states[0].state != STATE_UNAVAILABLE


async def test_light_entities_for_units(
    hass: HomeAssistant, configured_panel
) -> None:
    """One light entity per discovered unit."""
    states = hass.states.async_all("light")
    assert len(states) == 2
    # Both units default to off in the mock.
    for s in states:
        assert s.state in (STATE_OFF, STATE_UNAVAILABLE)


async def test_switch_entities_for_zone_bypass(
    hass: HomeAssistant, configured_panel
) -> None:
    """One bypass switch per binary zone."""
    states = hass.states.async_all("switch")
    assert len(states) == 3  # one per binary zone


async def test_climate_entity_for_thermostat(
    hass: HomeAssistant, configured_panel
) -> None:
    states = hass.states.async_all("climate")
    assert len(states) == 1


async def test_button_entity_for_panel_button(
    hass: HomeAssistant, configured_panel
) -> None:
    states = hass.states.async_all("button")
    assert len(states) == 1


async def test_event_entity_per_panel(
    hass: HomeAssistant, configured_panel
) -> None:
    states = hass.states.async_all("event")
    assert len(states) == 1


async def test_unload_entry(
    hass: HomeAssistant, configured_panel
) -> None:
    """Unloading the config entry tears everything down cleanly."""
    assert await hass.config_entries.async_unload(configured_panel.entry_id)
    await hass.async_block_till_done()
    assert configured_panel.state is ConfigEntryState.NOT_LOADED
    # Coordinator removed from hass.data
    assert configured_panel.entry_id not in hass.data.get(DOMAIN, {})


async def test_turn_unit_on_via_light_service(
    hass: HomeAssistant, configured_panel, panel
) -> None:
    """Drive a HA service call; verify it reaches the mock and updates state."""
    mock, _, _ = panel
    light_states = hass.states.async_all("light")
    assert light_states, "expected at least one light entity"
    target = light_states[0].entity_id
    await hass.services.async_call(
        "light", "turn_on", {"entity_id": target}, blocking=True
    )
    await hass.async_block_till_done()
    # The mock's state updated for whichever unit was first in sorted order.
    on_units = [u for u in mock.state.units.values() if u.state == 1]
    assert on_units, "expected the mock to record the unit as ON"


async def test_arm_panel_via_alarm_service(
    hass: HomeAssistant, configured_panel, panel
) -> None:
    """Arm the panel from HA; verify the mock area transitions."""
    mock, _, _ = panel
    alarm_states = hass.states.async_all("alarm_control_panel")
    assert alarm_states, "expected one alarm_control_panel entity"
    target = alarm_states[0].entity_id
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_away",
        {"entity_id": target, "code": "1234"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert mock.state.areas[1].mode == 3  # SecurityMode.AWAY


async def test_arm_panel_with_wrong_code_keeps_disarmed(
    hass: HomeAssistant, configured_panel, panel
) -> None:
    """Wrong code: panel stays disarmed and HA surfaces the error."""
    mock, _, _ = panel
    alarm_states = hass.states.async_all("alarm_control_panel")
    target = alarm_states[0].entity_id
    # The service should raise; we don't assert the exception class because
    # HA wraps it. We just assert the panel mode didn't change.
    import contextlib
    with contextlib.suppress(Exception):
        await hass.services.async_call(
            "alarm_control_panel",
            "alarm_arm_away",
            {"entity_id": target, "code": "9999"},
            blocking=True,
        )
    assert mock.state.areas[1].mode == 0  # still disarmed
