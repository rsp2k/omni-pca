#!/usr/bin/env python3
"""Add a *second* omni_pca config entry pointing at the real panel.

The dev stack already has one entry pointing at the mock panel
(``host.docker.internal:14369``). This script adds another entry for
the real panel at ``192.168.1.9:4369`` using ``transport=udp`` and the
controller key from the bundled .pca fixture.

Run inside the project venv:
    cd /home/kdm/home-auto/omni-pca
    uv run python dev/add_real_panel.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from probe_v1 import _load_key  # type: ignore  # noqa: E402

DEFAULT_HA_URL = "http://localhost:8123"
PANEL_HOST = "192.168.1.9"
PANEL_PORT = 4369


DEFAULT_USERNAME = "demo"
DEFAULT_PASSWORD = "demo-password-1234"


async def _get_token(ha_url: str) -> str:
    """Re-use the cached access token; otherwise log in via /auth/login_flow."""
    token_file = (
        Path(__file__).parent / "ha-config" / ".storage" / ".demo_access_token"
    )
    if token_file.exists():
        return token_file.read_text().strip()
    async with httpx.AsyncClient(base_url=ha_url, timeout=15.0) as client:
        r = await client.post(
            "/auth/login_flow",
            json={
                "client_id": ha_url,
                "handler": ["homeassistant", None],
                "redirect_uri": ha_url,
            },
        )
        r.raise_for_status()
        flow_id = r.json()["flow_id"]
        r = await client.post(
            f"/auth/login_flow/{flow_id}",
            json={
                "client_id": ha_url,
                "username": DEFAULT_USERNAME,
                "password": DEFAULT_PASSWORD,
            },
        )
        r.raise_for_status()
        auth_code = r.json()["result"]
        r = await client.post(
            "/auth/token",
            data={
                "client_id": ha_url,
                "grant_type": "authorization_code",
                "code": auth_code,
            },
        )
        r.raise_for_status()
        token = r.json()["access_token"]
        # Cache for next run.
        try:
            token_file.write_text(token)
        except Exception:
            pass
        return token


async def amain(args: argparse.Namespace) -> int:
    key_bytes = _load_key(None)
    key_hex = key_bytes.hex()
    print(f"[add-real-panel] target HA: {args.ha_url}")
    print(f"[add-real-panel] panel:     {PANEL_HOST}:{PANEL_PORT} (UDP)")
    print(f"[add-real-panel] key:       ...{key_hex[-4:]} (16 bytes)\n")

    token = await _get_token(args.ha_url)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=args.ha_url, timeout=30.0) as client:
        # ---- check if an entry already exists for this host ----
        r = await client.get(
            "/api/config/config_entries/entry", headers=headers
        )
        r.raise_for_status()
        for entry in r.json():
            if entry.get("domain") != "omni_pca":
                continue
            data = entry.get("data", {})
            if data.get("host") == PANEL_HOST and data.get("port") == PANEL_PORT:
                print(f"  already configured: {entry['title']} ({entry['entry_id']})")
                return 0

        # ---- start the config flow ----
        r = await client.post(
            "/api/config/config_entries/flow",
            headers=headers,
            json={"handler": "omni_pca", "show_advanced_options": False},
        )
        r.raise_for_status()
        flow = r.json()
        flow_id = flow["flow_id"]
        print(f"  flow opened: {flow_id} (step={flow.get('step_id')})")

        # ---- submit the form for the real panel ----
        r = await client.post(
            f"/api/config/config_entries/flow/{flow_id}",
            headers=headers,
            json={
                "host": PANEL_HOST,
                "port": PANEL_PORT,
                "controller_key": key_hex,
                "transport": "udp",
            },
            timeout=60.0,  # the probe round-trip can take a few seconds
        )
        r.raise_for_status()
        result = r.json()
        if result.get("type") == "create_entry":
            print(f"  ✓ entry created: {result.get('title')}")
            print(f"    entry_id:      {result.get('result')}")
        elif result.get("type") == "form":
            print(f"  form re-shown — errors: {result.get('errors')}")
            return 1
        else:
            print(f"  unexpected outcome: {result}")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
