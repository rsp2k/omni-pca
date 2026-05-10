"""Smoke-test that every module in the HA custom_component imports cleanly.

We don't want to require Home Assistant as a dev dependency just to lint
the package — if `homeassistant` isn't installed, skip the suite entirely.
This still catches typos / missing-module bugs in the integration as soon
as someone runs the tests in an HA-flavored env (or in CI with HA installed).
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("homeassistant")


@pytest.mark.parametrize(
    "module",
    [
        "custom_components.omni_pca",
        "custom_components.omni_pca.const",
        "custom_components.omni_pca.config_flow",
        "custom_components.omni_pca.coordinator",
        "custom_components.omni_pca.binary_sensor",
    ],
)
def test_module_imports(module: str) -> None:
    importlib.import_module(module)


def test_manifest_matches_library_version() -> None:
    """manifest.json must list the same omni-pca version we ship."""
    import json
    from importlib.metadata import version
    from pathlib import Path

    manifest_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "omni_pca"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    lib_version = version("omni-pca")
    assert manifest["version"] == lib_version, (
        f"manifest.json version {manifest['version']!r} != "
        f"library version {lib_version!r}"
    )
    assert f"omni-pca=={lib_version}" in manifest["requirements"]
