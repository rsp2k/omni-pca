#!/usr/bin/env python3
"""End-to-end demo: onboard HA, add the omni_pca integration against the
mock panel, drive playwright through the resulting UI to capture screenshots.

Run inside the project venv:
    uv run --with playwright --with httpx --with websockets \
        python dev/screenshot.py [--outdir DIR] [--ha-url URL]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

DEFAULT_HA_URL = "http://localhost:8123"
DEFAULT_USERNAME = "demo"
DEFAULT_PASSWORD = "demo-password-1234"
PANEL_HOST = "host.docker.internal"
PANEL_PORT = 14369
CONTROLLER_KEY = "000102030405060708090a0b0c0d0e0f"


async def _complete_onboarding(
    client: httpx.AsyncClient, headers: dict[str, str], ha_url: str
) -> None:
    """POST every remaining onboarding step in turn so HA stops greeting us."""
    r = await client.get("/api/onboarding")
    pending = [s["step"] for s in r.json() if not s.get("done")]
    print(f"  pending onboarding: {pending}")

    if "core_config" in pending:
        try:
            r = await client.post("/api/onboarding/core_config", headers=headers)
            print(f"    core_config -> {r.status_code}")
        except Exception as e:
            print(f"    core_config error: {e}")
    if "analytics" in pending:
        try:
            r = await client.post(
                "/api/onboarding/analytics",
                headers=headers,
                json={},
            )
            print(f"    analytics -> {r.status_code}")
        except Exception as e:
            print(f"    analytics error: {e}")
    if "integration" in pending:
        try:
            r = await client.post(
                "/api/onboarding/integration",
                headers=headers,
                json={"client_id": ha_url, "redirect_uri": ha_url},
            )
            print(f"    integration -> {r.status_code}")
        except Exception as e:
            print(f"    integration error: {e}")


async def _onboard(ha_url: str) -> str:
    """Run HA's onboarding REST flow if needed. Returns access token.

    HA uses the authorization_code OAuth flow. On first-run, POSTing to
    /api/onboarding/users returns an auth_code that we exchange for tokens.
    On subsequent runs, we use a long-lived access token created during
    the first run (persisted in ha-config/.storage/auth).
    """
    async with httpx.AsyncClient(base_url=ha_url, timeout=30.0) as client:
        r = await client.get("/api/onboarding")
        steps = r.json()
        user_step = next((s for s in steps if s["step"] == "user"), None)

        if user_step and not user_step.get("done"):
            # First-run path: create user, get auth_code, exchange.
            r = await client.post(
                "/api/onboarding/users",
                json={
                    "client_id": ha_url,
                    "name": "Demo User",
                    "username": DEFAULT_USERNAME,
                    "password": DEFAULT_PASSWORD,
                    "language": "en",
                },
            )
            r.raise_for_status()
            auth_code = r.json()["auth_code"]
            print("  ✓ user created")

            r = await client.post(
                "/auth/token",
                data={
                    "client_id": ha_url,
                    "grant_type": "authorization_code",
                    "code": auth_code,
                },
            )
            r.raise_for_status()
            access_token = r.json()["access_token"]

            # Complete the remaining onboarding steps so we land on the
            # dashboard rather than the discovery wizard.
            headers = {"Authorization": f"Bearer {access_token}"}
            await _complete_onboarding(client, headers, ha_url)
            return access_token

        # Subsequent-run path: log in via /auth/login_flow.
        token_file = (
            Path(__file__).parent / "ha-config" / ".storage" / ".demo_access_token"
        )
        if token_file.exists():
            print("  re-using cached demo access token")
            return token_file.read_text().strip()

        print("  logging in via /auth/login_flow")
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
        return r.json()["access_token"]


async def _add_integration(ha_url: str, token: str) -> None:
    """Add the omni_pca config entry via the REST config-flow endpoints."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(base_url=ha_url, timeout=30.0) as client:
        r = await client.get("/api/config/config_entries/entry", headers=headers)
        r.raise_for_status()
        for entry in r.json():
            if entry.get("domain") == "omni_pca":
                print(f"  integration already configured: {entry['title']}")
                return

        r = await client.post(
            "/api/config/config_entries/flow",
            headers=headers,
            json={"handler": "omni_pca", "show_advanced_options": False},
        )
        r.raise_for_status()
        flow = r.json()
        flow_id = flow["flow_id"]
        print(f"  config flow opened: {flow_id} (step={flow.get('step_id')})")

        r = await client.post(
            f"/api/config/config_entries/flow/{flow_id}",
            headers=headers,
            json={
                "host": PANEL_HOST,
                "port": PANEL_PORT,
                "controller_key": CONTROLLER_KEY,
            },
        )
        r.raise_for_status()
        result = r.json()
        if result.get("type") == "create_entry":
            print(f"  ✓ entry created: {result.get('title')}")
        else:
            raise RuntimeError(f"unexpected flow outcome: {result}")


async def _take_screenshots(ha_url: str, token: str, outdir: Path) -> list[Path]:
    """Drive playwright through a few interesting pages."""
    outdir.mkdir(parents=True, exist_ok=True)
    shots: list[Path] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=2,
        )
        # Inject auth so we skip the login screen.
        await context.add_init_script(
            f"""window.localStorage.setItem('hassTokens', JSON.stringify({{
                access_token: '{token}',
                token_type: 'Bearer',
                expires_in: 1800,
                hassUrl: '{ha_url}',
                clientId: '{ha_url}',
                expires: Date.now() + 1800000,
                refresh_token: 'placeholder',
            }}));
            window.localStorage.setItem('selectedLanguage', '"en"');
            """
        )
        page = await context.new_page()

        async def shot(filename: str, url: str, *, wait_for: str | None = None,
                       wait_secs: float = 4.0) -> None:
            print(f"  → {filename}  ({url})")
            try:
                await page.goto(f"{ha_url}{url}", wait_until="networkidle",
                                timeout=30000)
            except Exception as e:
                print(f"    nav warning: {e}")
            if wait_for:
                try:
                    await page.locator(wait_for).first.wait_for(timeout=10000)
                except Exception:
                    pass
            await page.wait_for_timeout(int(wait_secs * 1000))
            path = outdir / filename
            await page.screenshot(path=str(path), full_page=False)
            shots.append(path)

        # Make sure onboarding is fully complete before we screenshot anything.
        async with httpx.AsyncClient(base_url=ha_url, timeout=15.0) as client:
            await _complete_onboarding(
                client, {"Authorization": f"Bearer {token}"}, ha_url
            )

        # Look up the panel device id so we can deep-link to its page.
        device_id: str | None = None
        async with httpx.AsyncClient(base_url=ha_url, timeout=15.0) as client:
            r = await client.post(
                "/api/template",
                headers={"Authorization": f"Bearer {token}"},
                json={"template": "{{ device_id('sensor.omni_pro_ii_panel_model') }}"},
            )
            if r.status_code == 200:
                device_id = (r.text or "").strip().strip('"')
                if device_id and device_id != "None":
                    print(f"  panel device_id: {device_id}")
                else:
                    device_id = None

        await shot("01-overview.png", "/lovelace/0", wait_secs=5.0)
        await shot("02-integrations-list.png",
                   "/config/integrations/dashboard", wait_secs=4.0)
        await shot("03-omni-pca-config.png",
                   "/config/integrations/integration/omni_pca", wait_secs=4.0)
        if device_id:
            await shot("04-panel-device.png",
                       f"/config/devices/device/{device_id}", wait_secs=4.0)
        await shot(
            "05-entities-omni.png",
            '/config/entities?config_entry=' + 'omni_pca',
            wait_secs=4.0,
        )
        await shot("06-developer-states.png",
                   "/developer-tools/state", wait_secs=4.0)

        await browser.close()
    return shots


async def amain(args: argparse.Namespace) -> int:
    print("[1/3] HA onboarding...")
    token = await _onboard(args.ha_url)
    # Cache token so subsequent runs against an already-onboarded HA can reuse.
    token_file = (
        Path(__file__).parent / "ha-config" / ".storage" / ".demo_access_token"
    )
    try:
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(token)
    except Exception:
        pass

    print("[2/3] adding omni_pca integration...")
    await _add_integration(args.ha_url, token)
    # Give HA a moment to discover all entities.
    await asyncio.sleep(8)
    print("[3/3] capturing screenshots...")
    shots = await _take_screenshots(args.ha_url, token, args.outdir)
    print(f"\n✓ wrote {len(shots)} screenshots to {args.outdir}")
    for p in shots:
        print(f"   {p}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path(__file__).parent
        / "artifacts"
        / "screenshots"
        / datetime.now().strftime("%Y-%m-%d"),
    )
    args = parser.parse_args()
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
