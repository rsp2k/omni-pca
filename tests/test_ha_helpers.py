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
