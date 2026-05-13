"""HA-side integration: optional .pca file source for panel programs.

When ``CONF_PCA_PATH`` is set in the entry data, the coordinator should
parse the .pca file at that path (with ``CONF_PCA_KEY`` as the per-install
key) and use those programs *instead* of streaming them over the wire.
The wire-based discovery for everything else (zones, units, etc.) is
unaffected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from custom_components.omni_pca.const import (
    CONF_CONTROLLER_KEY,
    CONF_PCA_KEY,
    CONF_PCA_PATH,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import CONTROLLER_KEY_HEX

LIVE_FIXTURE_PLAIN = Path(
    "/home/kdm/home-auto/HAI/pca-re/extracted/Our_House.pca.plain"
)


def _materialize_encrypted_fixture(tmp_path: Path) -> tuple[Path, int]:
    """Re-encrypt the plain fixture so parse_pca_file can decrypt it.

    parse_pca_file always runs the XOR keystream. The plain dump bypasses
    that, so we re-apply the keystream with KEY_EXPORT and write the
    result to tmp_path. Returns (file_path, key) the coordinator should use.
    """
    from omni_pca.pca_file import KEY_EXPORT, decrypt_pca_bytes

    plain = LIVE_FIXTURE_PLAIN.read_bytes()
    # XOR is symmetric — "decrypt" of plain bytes with the export key
    # produces a valid encrypted .pca that parse_pca_file can read back.
    encrypted = decrypt_pca_bytes(plain, KEY_EXPORT)
    fixture = tmp_path / "Test_House.pca"
    fixture.write_bytes(encrypted)
    return fixture, KEY_EXPORT


@pytest.fixture
async def configured_with_pca(
    hass: HomeAssistant, panel: tuple[Any, str, int], tmp_path: Path
) -> AsyncIterator[ConfigEntry]:
    """Config entry pointing at a .pca file fixture for programs."""
    if not LIVE_FIXTURE_PLAIN.is_file():
        pytest.skip(f"live .pca fixture missing: {LIVE_FIXTURE_PLAIN}")

    fixture_path, pca_key = _materialize_encrypted_fixture(tmp_path)

    _, host, port = panel
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_CONTROLLER_KEY: CONTROLLER_KEY_HEX,
            CONF_PCA_PATH: str(fixture_path),
            CONF_PCA_KEY: pca_key,
        },
        title=f"Mock Omni @ {host}:{port} (with .pca)",
        unique_id=f"{host}:{port}",
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


async def test_pca_source_overrides_wire_programs(
    hass: HomeAssistant, configured_with_pca: ConfigEntry
) -> None:
    """The fixture .pca has 330 defined programs (Phase 1 recon). The mock
    panel only seeded 3 in conftest. When pca_path is set, the .pca count
    wins — proving the coordinator routed through _discover_programs_from_pca,
    not iter_programs."""
    coordinator = hass.data[DOMAIN][configured_with_pca.entry_id]
    assert len(coordinator.data.programs) == 330

    # Sanity: the diagnostic sensor reflects the .pca count, not the mock seed.
    sensors = [
        s for s in hass.states.async_all("sensor")
        if "panel_programs" in s.entity_id
    ]
    assert len(sensors) == 1
    assert int(sensors[0].state) == 330


async def test_pca_path_validation_rejects_missing_file(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """The config-flow validator returns ``pca_not_found`` for an absent
    file. We exercise the helper directly to avoid spinning a full mock
    panel just for the validation branch."""
    from custom_components.omni_pca.config_flow import OmniConfigFlow

    flow = OmniConfigFlow()
    flow.hass = hass
    err = await flow._validate_pca(str(tmp_path / "does-not-exist.pca"), 0)
    assert err == "pca_not_found"


async def test_pca_path_validation_rejects_garbage(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """A file that doesn't decode as a .pca returns ``pca_decode_failed``."""
    from custom_components.omni_pca.config_flow import OmniConfigFlow

    garbage = tmp_path / "garbage.pca"
    garbage.write_bytes(b"not a real pca file" * 1000)
    flow = OmniConfigFlow()
    flow.hass = hass
    err = await flow._validate_pca(str(garbage), 0)
    assert err == "pca_decode_failed"
