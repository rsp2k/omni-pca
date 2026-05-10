"""Pure-function tests for the controller-key validator.

These don't need a Home Assistant install — `parse_controller_key` is
intentionally extracted as a free function so it can be exercised in
isolation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make `custom_components` importable without requiring an installed HA.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Import directly from the module file (skipping the package __init__,
# which pulls in `homeassistant`). We load the module via spec so the
# test stays green even if HA isn't installed.

_CFG_FLOW_PATH = (
    _REPO_ROOT / "custom_components" / "omni_pca" / "config_flow.py"
)


def _load_parser():
    """Load just `parse_controller_key` without importing HA modules."""
    # Re-define the function inline by reading the source — keeps the test
    # self-contained without importing homeassistant.
    src = _CFG_FLOW_PATH.read_text()
    # Extract the function source between known markers.
    start = src.index("class InvalidControllerKey")
    end = src.index("_USER_SCHEMA")
    snippet = src[start:end]
    # Provide the constant the snippet relies on.
    namespace: dict = {"CONTROLLER_KEY_HEX_LEN": 32}
    exec(
        compile(snippet, str(_CFG_FLOW_PATH), "exec"),
        namespace,
    )
    return namespace["parse_controller_key"], namespace["InvalidControllerKey"]


parse_controller_key, InvalidControllerKey = _load_parser()


class TestParseControllerKey:
    def test_accepts_plain_hex(self) -> None:
        raw = "00112233445566778899aabbccddeeff"
        assert parse_controller_key(raw) == bytes.fromhex(raw)

    def test_accepts_uppercase(self) -> None:
        raw = "00112233445566778899AABBCCDDEEFF"
        assert parse_controller_key(raw) == bytes.fromhex(raw)

    def test_strips_0x_prefix(self) -> None:
        raw = "0x00112233445566778899aabbccddeeff"
        assert parse_controller_key(raw) == bytes.fromhex(raw[2:])

    def test_strips_separators(self) -> None:
        raw = "00:11:22:33:44:55:66:77:88:99:aa:bb:cc:dd:ee:ff"
        assert parse_controller_key(raw) == bytes.fromhex(raw.replace(":", ""))

    def test_strips_dashes_and_spaces(self) -> None:
        raw = "00-11 22-33 44-55 66-77 88-99 aa-bb cc-dd ee-ff"
        assert (
            parse_controller_key(raw)
            == bytes.fromhex(raw.replace("-", "").replace(" ", ""))
        )

    def test_returns_16_bytes(self) -> None:
        result = parse_controller_key("00" * 16)
        assert isinstance(result, bytes)
        assert len(result) == 16

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            "00",  # too short
            "00" * 17,  # too long
            "zz" * 16,  # not hex
            "0x" + "00" * 17,  # prefixed but too long
        ],
    )
    def test_rejects_bad_input(self, bad: str) -> None:
        with pytest.raises(InvalidControllerKey):
            parse_controller_key(bad)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(InvalidControllerKey):
            parse_controller_key(b"\x00" * 16)  # type: ignore[arg-type]
