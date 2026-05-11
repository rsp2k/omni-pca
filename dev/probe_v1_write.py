#!/usr/bin/env python3
"""Phase-2c live write smoke test: round-trip a no-op unit command.

Reads the current state of one unit, sends a command that should yield
the same observable result, then re-reads to confirm. Proves that
:meth:`OmniClientV1.execute_command` actually flows through the v1
Command opcode against the real panel without changing anything visible.

Run:
    cd /home/kdm/home-auto/omni-pca
    uv run python dev/probe_v1_write.py [--index N]

Default target is unit #4 ('STAIRS' per current panel config).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_v1 import _load_key  # type: ignore  # noqa: E402

from omni_pca.v1 import OmniClientV1


async def amain(args: argparse.Namespace) -> int:
    key = _load_key(args.key)
    print(f"[write probe] target {args.host}:{args.port}  unit #{args.index}\n")

    async with OmniClientV1(
        host=args.host, port=args.port, controller_key=key, timeout=4.0
    ) as c:
        before = (await c.get_unit_status(args.index, args.index))[args.index]
        print(f"BEFORE: state=0x{before.state:02x}  "
              f"brightness={before.brightness!r}  "
              f"time_remaining={before.time_remaining_secs}s")

        # Pick the safest no-op command for the unit's current state:
        # - state == 0 → send UNIT_OFF (already off, panel acks)
        # - state == 1 → send UNIT_ON (already on, panel acks)
        # - 100 <= state <= 200 → set_unit_level(percent) at the current level
        # - otherwise (scene/dim/etc.) → fall back to UNIT_ON which is harmless
        if before.state == 0:
            print("ACTION: turn_unit_off (already off, expecting Ack)")
            await c.turn_unit_off(args.index)
        elif before.state == 1:
            print("ACTION: turn_unit_on (already on, expecting Ack)")
            await c.turn_unit_on(args.index)
        elif 100 <= before.state <= 200:
            level = before.state - 100
            print(f"ACTION: set_unit_level({level}%) (already at this level)")
            await c.set_unit_level(args.index, level)
        else:
            print(f"ACTION: turn_unit_on (state=0x{before.state:02x} is exotic; safe ack expected)")
            await c.turn_unit_on(args.index)

        # Give the panel ~250ms to settle if it does pulse anything.
        await asyncio.sleep(0.25)

        after = (await c.get_unit_status(args.index, args.index))[args.index]
        print(f"AFTER:  state=0x{after.state:02x}  "
              f"brightness={after.brightness!r}  "
              f"time_remaining={after.time_remaining_secs}s")

        if after.state == before.state:
            print("\n✓ panel acked the Command, state unchanged — wire path verified")
        else:
            print(f"\n⚠ state changed (0x{before.state:02x} → 0x{after.state:02x}). "
                  "Probably harmless but worth investigating.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.9")
    parser.add_argument("--port", type=int, default=4369)
    parser.add_argument("--key", help="32 hex chars; overrides env/.omni_key")
    parser.add_argument("--index", type=int, default=4)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
