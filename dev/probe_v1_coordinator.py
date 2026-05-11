#!/usr/bin/env python3
"""Phase-3 smoke test: drive OmniClientV1Adapter through the same
sequence the HA coordinator runs in async_config_entry_first_refresh.

Doesn't pull in HA; just executes the discovery + initial poll pattern
against the real panel and prints what an OmniData snapshot would look
like. If this works, the actual HA coordinator should work too.

Run:
    cd /home/kdm/home-auto/omni-pca
    uv run python dev/probe_v1_coordinator.py
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_v1 import _load_key  # type: ignore  # noqa: E402

from omni_pca.models import ObjectType
from omni_pca.v1 import OmniClientV1Adapter


async def amain(args: argparse.Namespace) -> int:
    key = _load_key(args.key)
    print(f"[coord probe] target {args.host}:{args.port}\n")

    async with OmniClientV1Adapter(
        host=args.host, port=args.port, controller_key=key, timeout=10.0
    ) as c:
        # ---- 1. SystemInformation ----
        info = await c.get_system_information()
        print(f"=== SystemInformation ===\n"
              f"  model={info.model_name}  fw={info.firmware_version}\n")

        # ---- 2. Discovery: per-type names + synthesized properties ----
        print("=== Discovery (UploadNames stream + synth Properties) ===")
        zone_names = await c.list_zone_names()
        unit_names = await c.list_unit_names()
        area_names = await c.list_area_names()
        tstat_names = await c.list_thermostat_names()
        button_names = await c.list_button_names()
        print(f"  zones:        {len(zone_names)}")
        print(f"  units:        {len(unit_names)}")
        print(f"  areas:        {len(area_names)} (fallback if 0 streamed)")
        print(f"  thermostats:  {len(tstat_names)}")
        print(f"  buttons:      {len(button_names)}")
        print()

        # Sanity-check that get_object_properties returns a real dataclass
        # for one zone, one unit, one area, one thermostat, one button.
        for type_byte, name_dict, label in [
            (ObjectType.ZONE, zone_names, "Zone"),
            (ObjectType.UNIT, unit_names, "Unit"),
            (ObjectType.AREA, area_names, "Area"),
            (ObjectType.THERMOSTAT, tstat_names, "Thermostat"),
            (ObjectType.BUTTON, button_names, "Button"),
        ]:
            if not name_dict:
                print(f"  {label}: no entries, skipping property synth")
                continue
            idx = min(name_dict)
            props = await c.get_object_properties(type_byte, idx)
            print(f"  {label} #{idx}: {props}")
        print()

        # ---- 3. Polling: bulk status for each type, plus area derivation ----
        print("=== Polling (bulk status) ===")
        if zone_names:
            zone_end = max(zone_names)
            zones = await c.get_extended_status(ObjectType.ZONE, 1, zone_end)
            open_zones = [z for z in zones if getattr(z, "is_open", False)]
            print(f"  ZoneStatus[1..{zone_end}]: {len(zones)} records, "
                  f"{len(open_zones)} currently open")
        if unit_names:
            unit_end = max(unit_names)
            units = await c.get_extended_status(ObjectType.UNIT, 1, unit_end)
            on_units = [u for u in units if getattr(u, "is_on", False)]
            print(f"  UnitStatus[1..{unit_end}]: {len(units)} records, "
                  f"{len(on_units)} currently on")
        if tstat_names:
            tstat_end = max(tstat_names)
            tstats = await c.get_extended_status(
                ObjectType.THERMOSTAT, 1, tstat_end
            )
            print(f"  ThermostatStatus[1..{tstat_end}]: {len(tstats)} records")

        # Areas: derived from SystemStatus
        if area_names:
            area_end = max(area_names)
            areas = await c.get_object_status(ObjectType.AREA, 1, area_end)
            modes = [a.mode for a in areas]
            print(f"  AreaStatus[1..{area_end}]: {len(areas)} records, "
                  f"modes={modes}")
        print()

        # ---- 4. SystemStatus ----
        status = await c.get_system_status()
        print(f"=== SystemStatus ===\n"
              f"  panel_time={status.panel_time}  "
              f"battery=0x{status.battery_reading:02x}\n"
              f"  sunrise={status.sunrise_hour:02d}:{status.sunrise_minute:02d}  "
              f"sunset={status.sunset_hour:02d}:{status.sunset_minute:02d}\n")

    print("[coord probe] ✓ full discovery + poll cycle worked over v1+UDP")
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
