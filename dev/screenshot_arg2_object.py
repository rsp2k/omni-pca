#!/usr/bin/env python3
"""Focused screenshot of the structured-AND Arg2-as-object editor.

Drives an already-onboarded HA at localhost:8123, opens the side panel,
clicks into the chain at slot 200, hits Edit, and snaps the form.
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

HA_URL = "http://localhost:8123"
USERNAME = "demo"
PASSWORD = "demo-password-1234"


async def _login_token() -> str:
    async with httpx.AsyncClient(base_url=HA_URL, timeout=30) as c:
        r = await c.post(
            "/auth/login_flow",
            json={
                "client_id": HA_URL,
                "handler": ["homeassistant", None],
                "redirect_uri": HA_URL,
            },
        )
        flow_id = r.json()["flow_id"]
        r = await c.post(
            f"/auth/login_flow/{flow_id}",
            json={
                "username": USERNAME,
                "password": PASSWORD,
                "client_id": HA_URL,
            },
        )
        code = r.json()["result"]
        r = await c.post(
            "/auth/token",
            data={
                "client_id": HA_URL,
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        return r.json()["access_token"]


FIND_PANEL = """
  (() => {
    function find(root, depth=0) {
      if (!root || depth > 15) return null;
      if (root.tagName === 'OMNI-PANEL-PROGRAMS') return root;
      for (const k of Array.from(root.children || [])) {
        const r = find(k, depth+1);
        if (r) return r;
      }
      if (root.shadowRoot) {
        const r = find(root.shadowRoot, depth+1);
        if (r) return r;
      }
      return null;
    }
    return find(document.body);
  })()
"""


async def amain(outdir: Path) -> None:
    token = await _login_token()
    outdir.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        context = await browser.new_context(viewport={"width": 1400, "height": 900})
        await context.add_init_script(f"""
          window.localStorage.setItem(
            'hassTokens',
            JSON.stringify({{
              access_token: '{token}',
              token_type: 'Bearer',
              refresh_token: '',
              expires: Date.now() + 3600000,
              hassUrl: '{HA_URL}',
              clientId: '{HA_URL}',
            }})
          );
          window.localStorage.setItem('selectedTheme', JSON.stringify({{dark: false}}));
        """)
        page = await context.new_page()

        page.on("console", lambda m: print(f"  [browser] {m.type}: {m.text}"))

        await page.goto(f"{HA_URL}/omni-panel-programs", wait_until="domcontentloaded")
        await page.wait_for_timeout(6000)

        # Click the chain row (slot 200).
        ok = await page.evaluate(f"""() => {{
          const panel = {FIND_PANEL};
          if (!panel) return 'no-panel';
          const rows = Array.from(panel.shadowRoot.querySelectorAll('.row'));
          const target = rows.find(r => r.textContent.includes('200'));
          if (!target) return 'no-row-200 ' + rows.map(r => r.textContent.slice(0,40)).join(' | ');
          target.click();
          return 'clicked';
        }}""")
        print(f"  row-click: {ok}")
        await page.wait_for_timeout(800)

        # Click Edit.
        ok = await page.evaluate(f"""() => {{
          const panel = {FIND_PANEL};
          if (!panel) return 'no-panel';
          const buttons = panel.shadowRoot.querySelectorAll('.detail button');
          for (const b of buttons) {{
            if (b.textContent.trim() === 'Edit') {{ b.click(); return 'clicked'; }}
          }}
          return 'no-edit-button';
        }}""")
        print(f"  edit-click: {ok}")
        await page.wait_for_timeout(1500)

        path = outdir / "arg2-object-editor.png"
        await page.screenshot(path=str(path), full_page=True)
        print(f"  wrote {path}")

        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--outdir",
        type=Path,
        default=Path(__file__).parent / "artifacts" / "screenshots" /
            datetime.now().strftime("%Y-%m-%d"),
    )
    args = parser.parse_args()
    asyncio.run(amain(args.outdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
