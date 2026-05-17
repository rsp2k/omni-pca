#!/usr/bin/env python3
"""Launch a long-running MockPanel suitable for the docker-compose dev stack.

Reuses the mock fixture from the test suite so the behaviour matches what
the HA integration tests prove out. Defaults match dev/docker-compose.yml.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from omni_pca.mock_panel import (
    MockAreaState,
    MockButtonState,
    MockPanel,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)
from omni_pca.commands import Command
from omni_pca.pca_file import KEY_EXPORT, parse_pca01_cfg
from omni_pca.programs import Days, Program, ProgramType

DEFAULT_KEY_HEX = "000102030405060708090a0b0c0d0e0f"


def _seed_programs() -> dict[int, bytes]:
    """A handful of programs covering compact-form + clausal-chain shapes.

    Slot 200..202 is a chain with a structured-AND condition whose Arg2
    is itself a Thermostat reference — exercises the Arg2-as-object
    editor controls.
    """
    programs: dict[int, Program] = {
        12: Program(
            slot=12, prog_type=int(ProgramType.TIMED),
            cmd=int(Command.UNIT_ON), pr2=1,
            hour=6, minute=0, days=int(Days.MONDAY | Days.FRIDAY),
        ),
        42: Program(
            slot=42, prog_type=int(ProgramType.TIMED),
            cmd=int(Command.UNIT_OFF), pr2=2,
            hour=22, minute=30, days=int(Days.SUNDAY),
        ),
        # Chain: WHEN zone 1 not-ready, AND IF Thermostat 1.Temp >
        # Thermostat 2.Temp, THEN turn ON unit 3. The AND record is a
        # structured-OP comparison with Arg2 as a Thermostat reference.
        200: Program(
            slot=200, prog_type=int(ProgramType.WHEN),
            month=0x04, day=0x01,
        ),
        201: Program(
            slot=201, prog_type=int(ProgramType.AND),
            cond=(4 << 8) | 4,   # op=GT (4), arg1Type=Thermostat (4)
            cond2=1,             # arg1Ix=1
            cmd=1,               # arg1Field=current temp
            par=4,               # arg2Type=Thermostat (4)
            pr2=2,               # arg2Ix=2
            month=1,             # arg2Field=current temp
        ),
        202: Program(
            slot=202, prog_type=int(ProgramType.THEN),
            cmd=int(Command.UNIT_ON), pr2=3,
        ),
    }
    return {slot: p.encode_wire_bytes() for slot, p in programs.items()}


def _populated_state() -> MockState:
    """A small but representative set of objects so HA shows real entities."""
    return MockState(
        zones={
            1: MockZoneState(name="FRONT_DOOR"),
            2: MockZoneState(name="GARAGE_ENTRY"),
            3: MockZoneState(name="BACK_DOOR"),
            10: MockZoneState(name="LIVING_MOTION"),
            11: MockZoneState(name="HALL_MOTION"),
        },
        units={
            1: MockUnitState(name="LIVING_LAMP"),
            2: MockUnitState(name="KITCHEN_OVERHEAD"),
            3: MockUnitState(name="FRONT_PORCH"),
            4: MockUnitState(name="BEDROOM_FAN"),
        },
        areas={
            1: MockAreaState(name="MAIN"),
            2: MockAreaState(name="GUEST"),
        },
        thermostats={
            1: MockThermostatState(name="LIVING_ROOM"),
            2: MockThermostatState(name="MASTER_BEDROOM"),
        },
        buttons={
            1: MockButtonState(name="GOOD_MORNING"),
            2: MockButtonState(name="MOVIE_MODE"),
            3: MockButtonState(name="GOODNIGHT"),
        },
        user_codes={1: 1234, 2: 5678},
        programs=_seed_programs(),
    )


def _key_for_pca(path: Path, override: int | None) -> int:
    """Pick the decryption key for a .pca file.

    Priority:
      1. Explicit override (CLI / env var).
      2. Per-installation key from a sibling ``PCA01.CFG`` (most common —
         PC Access ships each export with a matching config file).
      3. ``KEY_EXPORT`` as a last resort for vanilla exports.
    """
    if override is not None:
        return override
    cfg_path = path.parent / "PCA01.CFG"
    if cfg_path.is_file():
        cfg = parse_pca01_cfg(cfg_path.read_bytes())
        logging.info("derived pca_key from %s: 0x%08X", cfg_path.name, cfg.pca_key)
        return cfg.pca_key
    logging.info("no sibling PCA01.CFG; falling back to KEY_EXPORT")
    return KEY_EXPORT


def _state_from_pca(path: Path, key: int) -> MockState:
    """Seed a MockState from a real .pca file."""
    state = MockState.from_pca(str(path), key=key)
    logging.info(
        "loaded %s: %d zones, %d units, %d areas, %d thermostats, %d programs",
        path.name,
        len(state.zones), len(state.units), len(state.areas),
        len(state.thermostats), len(state.programs),
    )
    return state


async def _serve(
    host: str, port: int, key: bytes, pca: Path | None, pca_key: int | None,
) -> None:
    if pca is not None:
        state = _state_from_pca(pca, _key_for_pca(pca, pca_key))
    else:
        state = _populated_state()
    panel = MockPanel(controller_key=key, state=state)
    async with panel.serve(host=host, port=port) as (bound_host, bound_port):
        logging.info("MockPanel listening on %s:%d", bound_host, bound_port)
        logging.info("Use this controller key in HA: %s", key.hex())
        stop = asyncio.Event()

        def _on_signal() -> None:
            logging.info("shutdown signal received")
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _on_signal)

        await stop.wait()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=14369)
    parser.add_argument(
        "--controller-key",
        default=DEFAULT_KEY_HEX,
        help="32 hex chars; default is the docker-compose value",
    )
    parser.add_argument(
        "--pca",
        default=os.environ.get("OMNI_PCA_FIXTURE"),
        help="Path to a .pca file. When supplied, the mock seeds its "
             "state from this file instead of the hard-coded sample. "
             "Can also be set via OMNI_PCA_FIXTURE.",
    )
    parser.add_argument(
        "--pca-key",
        type=lambda s: int(s, 0),
        default=(
            int(os.environ["OMNI_PCA_FIXTURE_KEY"], 0)
            if os.environ.get("OMNI_PCA_FIXTURE_KEY") else None
        ),
        help="32-bit decryption key for --pca. Default: auto-derive from "
             "a sibling PCA01.CFG, or fall back to KEY_EXPORT.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        key = bytes.fromhex(args.controller_key)
    except ValueError:
        print(f"controller-key must be 32 hex chars: {args.controller_key!r}",
              file=sys.stderr)
        return 2
    if len(key) != 16:
        print(f"controller-key must decode to exactly 16 bytes (got {len(key)})",
              file=sys.stderr)
        return 2

    pca_path: Path | None = None
    if args.pca:
        pca_path = Path(args.pca)
        if not pca_path.is_file():
            print(f"--pca path not found: {pca_path}", file=sys.stderr)
            return 2

    asyncio.run(_serve(args.host, args.port, key, pca_path, args.pca_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
