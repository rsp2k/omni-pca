"""Pure-function tests for ``custom_components.omni_pca.helpers``.

These never import anything from ``homeassistant.*``, so they run in the
same venv as the rest of the library tests. The HA-bound modules
(coordinator, binary_sensor, __init__) are covered separately by
``test_ha_imports.py`` which uses ``pytest.importorskip("homeassistant")``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load the helpers module by file path so we don't have to drag in the
# rest of the package (which imports `homeassistant.*` at module scope).
_REPO_ROOT = Path(__file__).parent.parent
_HELPERS_PATH = _REPO_ROOT / "custom_components" / "omni_pca" / "helpers.py"


def _load_helpers():
    spec = importlib.util.spec_from_file_location(
        "_omni_pca_helpers_under_test", _HELPERS_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


class TestDeviceClassForZoneType:
    @pytest.mark.parametrize(
        ("zone_type", "expected"),
        [
            (0, "opening"),    # ENTRY_EXIT
            (1, "opening"),    # PERIMETER
            (2, "motion"),     # NIGHT_INTERIOR
            (3, "motion"),     # AWAY_INTERIOR
            (16, "safety"),    # PANIC
            (17, "safety"),    # POLICE_EMERGENCY
            (18, "safety"),    # SILENT_DURESS
            (19, "tamper"),    # TAMPER
            (20, "tamper"),    # LATCHING_TAMPER
            (32, "smoke"),     # FIRE
            (33, "smoke"),     # FIRE_EMERGENCY
            (34, "gas"),       # GAS
            (54, "cold"),      # FREEZE
            (55, "moisture"),  # WATER
            (56, "tamper"),    # FIRE_TAMPER
        ],
    )
    def test_known_zone_types(self, zone_type: int, expected: str) -> None:
        assert helpers.device_class_for_zone_type(zone_type) == expected

    def test_unknown_zone_type_defaults_to_opening(self) -> None:
        assert helpers.device_class_for_zone_type(199) == "opening"

    def test_zero_is_opening(self) -> None:
        assert helpers.device_class_for_zone_type(0) == "opening"


class TestIsBinaryZoneType:
    @pytest.mark.parametrize("analog_type", [80, 81, 82, 83, 84])
    def test_analog_types_excluded(self, analog_type: int) -> None:
        assert helpers.is_binary_zone_type(analog_type) is False

    @pytest.mark.parametrize(
        "binary_type", [0, 1, 2, 3, 16, 19, 32, 34, 54, 55, 56, 64]
    )
    def test_binary_types_included(self, binary_type: int) -> None:
        assert helpers.is_binary_zone_type(binary_type) is True


class TestUseLatchedAlarmForZone:
    @pytest.mark.parametrize(
        "latching_type",
        [16, 17, 18, 19, 20, 32, 33, 34, 48, 54, 55, 56],
    )
    def test_latching_types(self, latching_type: int) -> None:
        assert helpers.use_latched_alarm_for_zone(latching_type) is True

    @pytest.mark.parametrize("contact_type", [0, 1, 2, 3, 4, 5, 6, 7, 8])
    def test_contact_and_motion_types_use_current_condition(
        self, contact_type: int
    ) -> None:
        assert helpers.use_latched_alarm_for_zone(contact_type) is False


class TestPrettifyName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("FRONT_DOOR", "Front Door"),
            ("front_door", "Front Door"),
            ("KITCHEN", "Kitchen"),
            ("  Trimmed  ", "Trimmed"),
            ("MOTION_KIDS_ROOM", "Motion Kids Room"),
            ("", ""),
        ],
    )
    def test_round_trip(self, raw: str, expected: str) -> None:
        assert helpers.prettify_name(raw) == expected


# ----------------------------------------------------------------------------
# Phase B helpers — pure functions used by the new entity platforms
# ----------------------------------------------------------------------------


class TestSecurityModeToAlarmState:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (0, "disarmed"),
            (1, "armed_home"),       # DAY
            (2, "armed_night"),      # NIGHT
            (3, "armed_away"),       # AWAY
            (4, "armed_vacation"),   # VACATION
            (5, "armed_custom_bypass"),  # DAY_INSTANT
            (6, "armed_night"),      # NIGHT_DELAYED
        ],
    )
    def test_steady_state(self, mode: int, expected: str) -> None:
        assert helpers.security_mode_to_alarm_state(mode) == expected

    def test_alarm_active_overrides(self) -> None:
        assert helpers.security_mode_to_alarm_state(3, alarm_active=True) == "triggered"

    def test_entry_timer_pending(self) -> None:
        assert helpers.security_mode_to_alarm_state(3, entry_timer=15) == "pending"

    def test_exit_timer_arming(self) -> None:
        assert helpers.security_mode_to_alarm_state(3, exit_timer=30) == "arming"

    @pytest.mark.parametrize("arming_mode", [9, 10, 11, 12, 13, 14])
    def test_arming_in_progress_modes(self, arming_mode: int) -> None:
        assert helpers.security_mode_to_alarm_state(arming_mode) == "arming"

    def test_unknown_mode_falls_back_to_disarmed(self) -> None:
        assert helpers.security_mode_to_alarm_state(99) == "disarmed"


class TestArmServiceMapping:
    def test_all_services_present(self) -> None:
        for svc in ("arm_home", "arm_away", "arm_night", "arm_vacation",
                    "arm_custom_bypass", "disarm"):
            assert svc in helpers.ARM_SERVICE_TO_SECURITY_MODE

    def test_round_trip_through_alarm_state(self) -> None:
        # Arm with each service, decode the resulting mode back to the HA
        # state, and verify the names are sensible.
        for svc, expected_state in [
            ("disarm", "disarmed"),
            ("arm_home", "armed_home"),
            ("arm_away", "armed_away"),
            ("arm_night", "armed_night"),
            ("arm_vacation", "armed_vacation"),
        ]:
            mode = helpers.ARM_SERVICE_TO_SECURITY_MODE[svc]
            assert helpers.security_mode_to_alarm_state(mode) == expected_state


class TestBrightnessConversions:
    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (0, None),       # off
            (1, 255),        # plain on, non-dimmable
            (100, 1),        # 0% via Omni's overlap (level 100 = 0%, but we floor at 1)
            (150, 128),      # 50%
            (200, 255),      # 100%
        ],
    )
    def test_state_to_ha_brightness(self, state: int, expected: int | None) -> None:
        result = helpers.omni_state_to_ha_brightness(state)
        assert result == expected

    @pytest.mark.parametrize(
        ("brightness", "expected_percent"),
        [
            (1, 1),
            (128, 50),
            (255, 100),
        ],
    )
    def test_ha_brightness_to_omni_percent(
        self, brightness: int, expected_percent: int
    ) -> None:
        assert helpers.ha_brightness_to_omni_percent(brightness) == expected_percent

    def test_zero_brightness(self) -> None:
        assert helpers.ha_brightness_to_omni_percent(0) == 0


class TestHvacFanHoldRoundTrip:
    @pytest.mark.parametrize(
        ("omni_mode", "expected_ha"),
        [(0, "off"), (1, "heat"), (2, "cool"), (3, "heat_cool"), (4, "heat")],
    )
    def test_hvac_mapping(self, omni_mode: int, expected_ha: str) -> None:
        assert helpers.omni_hvac_to_ha(omni_mode) == expected_ha

    @pytest.mark.parametrize(
        ("ha_mode", "expected_omni"),
        [("off", 0), ("heat", 1), ("cool", 2), ("heat_cool", 3)],
    )
    def test_hvac_inverse(self, ha_mode: str, expected_omni: int) -> None:
        assert helpers.ha_hvac_to_omni(ha_mode) == expected_omni

    def test_fan_round_trip(self) -> None:
        for omni in (0, 1, 2):
            ha = helpers.omni_fan_to_ha(omni)
            back = helpers.ha_fan_to_omni(ha)
            assert back == omni

    def test_hold_round_trip(self) -> None:
        for omni in (0, 1, 2):
            ha = helpers.omni_hold_to_ha(omni)
            back = helpers.ha_hold_to_omni(ha)
            assert back == omni

    def test_legacy_old_on_hold_value(self) -> None:
        # Old firmware sentinel 0xFF should map to the same HA preset as 1.
        assert helpers.omni_hold_to_ha(0xFF) == helpers.omni_hold_to_ha(1)


class TestTemperatureInverse:
    @pytest.mark.parametrize(
        ("fahrenheit", "expected_raw"),
        [
            (-40, 0),    # bottom of the scale
            (0, 44),     # ~0°F
            (32, 80),    # freezing
            (72, 124),   # room temp
            (212, 280),  # boiling — above byte range, gets clamped
        ],
    )
    def test_fahrenheit_to_raw(self, fahrenheit: float, expected_raw: int) -> None:
        result = helpers.fahrenheit_to_omni_raw(fahrenheit)
        # We clamp to 0..255 so 212°F (would compute 280) becomes 255.
        if expected_raw > 255:
            assert result == 255
        else:
            assert result == expected_raw

    def test_inverse_round_trip_at_typical_setpoints(self) -> None:
        # Take a few raw values, decode to °F via the linear formula, encode
        # back, and verify we get the same byte (within ±1 due to rounding).
        for raw in (80, 100, 124, 144, 168, 184):
            fahrenheit = round(raw * 9 / 10) - 40
            back = helpers.fahrenheit_to_omni_raw(fahrenheit)
            assert abs(back - raw) <= 1


class TestAnalogZoneDeviceClass:
    @pytest.mark.parametrize(
        ("zone_type", "expected"),
        [
            (80, "power"),
            (81, "temperature"),
            (82, "temperature"),
            (83, "temperature"),
            (84, "humidity"),
            (1, None),       # binary zone — not analog
            (255, None),     # unknown
        ],
    )
    def test_mapping(self, zone_type: int, expected: str | None) -> None:
        assert helpers.analog_zone_device_class(zone_type) == expected


class TestEventTypeFor:
    @pytest.mark.parametrize(
        ("class_name", "expected"),
        [
            ("ZoneStateChanged", "zone_state_changed"),
            ("UnitStateChanged", "unit_state_changed"),
            ("ArmingChanged", "arming_changed"),
            ("AlarmActivated", "alarm_activated"),
            ("AlarmCleared", "alarm_cleared"),
            ("AcLost", "ac_lost"),
            ("AcRestored", "ac_restored"),
            ("BatteryLow", "battery_low"),
            ("BatteryRestored", "battery_restored"),
            ("UserMacroButton", "user_macro_button"),
            ("PhoneLineDead", "phone_line_dead"),
            ("PhoneLineRestored", "phone_line_restored"),
        ],
    )
    def test_known_events(self, class_name: str, expected: str) -> None:
        assert helpers.event_type_for(class_name) == expected

    def test_unknown_event_class(self) -> None:
        assert helpers.event_type_for("SomeRandomThing") == "unknown"

    def test_event_types_tuple_includes_unknown(self) -> None:
        assert "unknown" in helpers.EVENT_TYPES
