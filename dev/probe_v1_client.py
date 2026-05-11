#!/usr/bin/env python3
"""Phase-2a smoke test: drive OmniClientV1 against the real panel.

Hits the read-only methods we care about for HA polling. Compares parsed
values against the recon dump so we catch off-by-one byte errors fast.

Run:
    cd /home/kdm/home-auto/omni-pca
    uv run python dev/probe_v1_client.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_v1 import _load_key  # type: ignore  # noqa: E402

from omni_pca.v1 import OmniClientV1, OmniNakError


async def amain(args: argparse.Namespace) -> int:
    key = _load_key(args.key)
    print(f"[client probe] target {args.host}:{args.port}\n")

    async with OmniClientV1(
        host=args.host, port=args.port, controller_key=key, timeout=4.0,
    ) as c:
        info = await c.get_system_information()
        print(f"system: model={info.model_name}  fw={info.firmware_version}  "
              f"phone={info.local_phone!r}")

        print("\n--- discovery (streaming UploadNames) ---")
        all_names = await c.list_all_names()
        for type_byte in sorted(all_names):
            try:
                from omni_pca.v1 import NameType
                label = NameType(type_byte).name
            except ValueError:
                label = f"type{type_byte}"
            print(f"  {label} ({len(all_names[type_byte])} entries)")
            for num in sorted(all_names[type_byte]):
                print(f"    #{num}: {all_names[type_byte][num]!r}")

        try:
            sysstatus = await c.get_system_status()
            print(f"status: time={sysstatus.panel_time}  "
                  f"battery=0x{sysstatus.battery_reading:02x}  "
                  f"sunrise={sysstatus.sunrise_hour:02d}:{sysstatus.sunrise_minute:02d}  "
                  f"sunset={sysstatus.sunset_hour:02d}:{sysstatus.sunset_minute:02d}  "
                  f"area_modes={[m for m, _ in sysstatus.area_alarms]}")
        except Exception as exc:
            print(f"system status failed: {type(exc).__name__}: {exc}")

        print("\n--- zones 1..16 ---")
        zones = await c.get_zone_status(1, 16)
        for idx in sorted(zones):
            z = zones[idx]
            flags = []
            if z.is_open: flags.append("open")
            if z.is_in_alarm: flags.append("alarm")
            if z.is_bypassed: flags.append("bypass")
            if z.is_trouble: flags.append("trouble")
            tag = ",".join(flags) or "secure"
            print(f"  zone {idx:2d}: status=0x{z.raw_status:02x} loop=0x{z.loop:02x} ({tag})")

        print("\n--- units 1..16 ---")
        units = await c.get_unit_status(1, 16)
        for idx in sorted(units):
            u = units[idx]
            br = u.brightness
            br_s = f"{br}%" if br is not None else "n/a"
            print(f"  unit {idx:2d}: state=0x{u.state:02x} ({br_s}) "
                  f"time_remaining={u.time_remaining_secs}s")

        print("\n--- thermostats 1..4 ---")
        try:
            tstats = await c.get_thermostat_status(1, 4)
            for idx in sorted(tstats):
                t = tstats[idx]
                print(f"  tstat {idx}: status=0x{t.status:02x} "
                      f"temp_F={t.temperature_f:.1f}  "
                      f"heat={t.heat_setpoint_f:.0f}  cool={t.cool_setpoint_f:.0f}  "
                      f"mode=0x{t.system_mode:02x} fan=0x{t.fan_mode:02x} "
                      f"hold=0x{t.hold_mode:02x}")
        except OmniNakError as exc:
            print(f"  no thermostats configured: {exc}")

        print("\n--- aux 1..8 ---")
        try:
            auxes = await c.get_aux_status(1, 8)
            for idx in sorted(auxes):
                a = auxes[idx]
                print(f"  aux {idx}: output=0x{a.output:02x} value=0x{a.value_raw:02x} "
                      f"low=0x{a.low_raw:02x} high=0x{a.high_raw:02x}")
        except OmniNakError as exc:
            print(f"  no aux sensors: {exc}")

    print("\n[client probe] ✓ disconnected cleanly")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.9")
    parser.add_argument("--port", type=int, default=4369)
    parser.add_argument("--key", help="32 hex chars; overrides env/.omni_key")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
