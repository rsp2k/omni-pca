#!/usr/bin/env python3
"""Quick screenshot of the Omni Programs side panel landing page."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

HA_URL = "http://localhost:8123"
USERNAME = "demo"
PASSWORD = "demo-password-1234"


async def _login_token() -> str:
    async with httpx.AsyncClient(base_url=HA_URL, timeout=30) as c:
        r = await c.post("/auth/login_flow", json={
            "client_id": HA_URL, "handler": ["homeassistant", None],
            "redirect_uri": HA_URL,
        })
        flow_id = r.json()["flow_id"]
        r = await c.post(f"/auth/login_flow/{flow_id}", json={
            "username": USERNAME, "password": PASSWORD, "client_id": HA_URL,
        })
        code = r.json()["result"]
        r = await c.post("/auth/token", data={
            "client_id": HA_URL, "grant_type": "authorization_code", "code": code,
        })
        return r.json()["access_token"]


async def amain(outdir: Path) -> None:
    token = await _login_token()
    outdir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await context.add_init_script(f"""
          window.localStorage.setItem('hassTokens', JSON.stringify({{
            access_token: '{token}', token_type: 'Bearer', refresh_token: '',
            expires: Date.now() + 3600000, hassUrl: '{HA_URL}', clientId: '{HA_URL}',
          }}));
          window.localStorage.setItem('selectedTheme', JSON.stringify({{dark: false}}));
        """)
        page = await context.new_page()
        await page.goto(f"{HA_URL}/omni-panel-programs", wait_until="domcontentloaded")
        await page.wait_for_timeout(8000)
        path = outdir / "real-pca-overview.png"
        await page.screenshot(path=str(path), full_page=True)
        print(f"  wrote {path}")
        await browser.close()


if __name__ == "__main__":
    outdir = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        Path(__file__).parent / "artifacts" / "screenshots" /
        datetime.now().strftime("%Y-%m-%d")
    )
    asyncio.run(amain(outdir))
