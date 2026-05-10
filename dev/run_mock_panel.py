#!/usr/bin/env python3
"""Launch a long-running MockPanel suitable for the docker-compose dev stack.

Reuses the mock fixture from the test suite so the behaviour matches what
the HA integration tests prove out. Defaults match dev/docker-compose.yml.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from omni_pca.mock_panel import (
    MockAreaState,
    MockButtonState,
    MockPanel,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)

DEFAULT_KEY_HEX = "000102030405060708090a0b0c0d0e0f"


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
    )


async def _serve(host: str, port: int, key: bytes) -> None:
    panel = MockPanel(controller_key=key, state=_populated_state())
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

    asyncio.run(_serve(args.host, args.port, key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
