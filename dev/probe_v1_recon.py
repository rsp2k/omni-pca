#!/usr/bin/env python3
"""Phase-2 reconnaissance: fetch v1 status replies from the real panel.

Doesn't parse — just dumps the raw payload bytes for each known v1 opcode
so we can match them against the C# message classes before writing
parsers. Builds the picture of what your panel actually has configured.

Run:
    cd /home/kdm/home-auto/omni-pca
    uv run python dev/probe_v1_recon.py [--debug]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Reuse the key loader from probe_v1.
sys.path.insert(0, str(Path(__file__).parent))
from probe_v1 import _load_key  # type: ignore  # noqa: E402

from omni_pca.opcodes import OmniLinkMessageType
from omni_pca.v1.connection import OmniConnectionV1, RequestTimeoutError


async def _request_or_warn(
    conn: OmniConnectionV1,
    label: str,
    opcode: OmniLinkMessageType,
    payload: bytes = b"",
    expected_opcode: int | None = None,
) -> None:
    print(f"--- {label} (req opcode {int(opcode)}, payload {payload.hex() or '<empty>'}) ---")
    try:
        reply = await conn.request(opcode, payload, timeout=4.0)
    except RequestTimeoutError as exc:
        print(f"    TIMEOUT: {exc}")
        return
    except Exception as exc:
        print(f"    ERROR:   {type(exc).__name__}: {exc}")
        return
    print(f"    reply opcode = {reply.opcode}")
    print(f"    payload ({len(reply.payload)} B) = {reply.payload.hex()}")
    if expected_opcode is not None and reply.opcode != expected_opcode:
        print(f"    NOTE: expected opcode {expected_opcode}, got {reply.opcode}")


async def amain(args: argparse.Namespace) -> int:
    key = _load_key(args.key)
    print(f"[recon] target {args.host}:{args.port}\n")

    async with OmniConnectionV1(
        host=args.host,
        port=args.port,
        controller_key=key,
        timeout=4.0,
        retry_count=1,
    ) as conn:
        print(f"handshake OK  state={conn.state.name}\n")

        # --- panel-wide ---
        await _request_or_warn(
            conn, "SystemInformation", OmniLinkMessageType.RequestSystemInformation,
            expected_opcode=int(OmniLinkMessageType.SystemInformation),
        )
        await _request_or_warn(
            conn, "SystemStatus", OmniLinkMessageType.RequestSystemStatus,
            expected_opcode=int(OmniLinkMessageType.SystemStatus),
        )
        await _request_or_warn(
            conn, "StatusSummary", OmniLinkMessageType.RequestStatusSummary,
            expected_opcode=int(OmniLinkMessageType.StatusSummary),
        )

        # --- bulk status, small ranges so we can read the bytes ---
        await _request_or_warn(
            conn, "ZoneStatus[1..8]", OmniLinkMessageType.RequestZoneStatus,
            payload=bytes([1, 8]),
            expected_opcode=int(OmniLinkMessageType.ZoneStatus),
        )
        await _request_or_warn(
            conn, "ZoneExtendedStatus[1..8]", OmniLinkMessageType.RequestZoneExtendedStatus,
            payload=bytes([1, 8]),
            expected_opcode=int(OmniLinkMessageType.ZoneExtendedStatus),
        )
        await _request_or_warn(
            conn, "UnitStatus[1..8]", OmniLinkMessageType.RequestUnitStatus,
            payload=bytes([1, 8]),
            expected_opcode=int(OmniLinkMessageType.UnitStatus),
        )
        await _request_or_warn(
            conn, "UnitExtendedStatus[1..8]", OmniLinkMessageType.RequestUnitExtendedStatus,
            payload=bytes([1, 8]),
            expected_opcode=int(OmniLinkMessageType.UnitExtendedStatus),
        )
        await _request_or_warn(
            conn, "ThermostatStatus[1..4]", OmniLinkMessageType.RequestThermostatStatus,
            payload=bytes([1, 4]),
            expected_opcode=int(OmniLinkMessageType.ThermostatStatus),
        )
        await _request_or_warn(
            conn, "ThermostatExtendedStatus[1..4]", OmniLinkMessageType.RequestThermostatExtendedStatus,
            payload=bytes([1, 4]),
            expected_opcode=int(OmniLinkMessageType.ThermostatExtendedStatus),
        )
        await _request_or_warn(
            conn, "AuxiliaryStatus[1..8]", OmniLinkMessageType.RequestAuxiliaryStatus,
            payload=bytes([1, 8]),
            expected_opcode=int(OmniLinkMessageType.AuxiliaryStatus),
        )

        # --- discovery: UploadNames is the READ request; DownloadNames is the
        # WRITE direction (panel <- client). Reply payload is NameData with the
        # next defined object's number + name.
        # Per clsOL2MsgUploadNames: [type, num_hi, num_lo, relative_direction].
        # type: 1=Zone 2=Unit 3=Button 4=Code 5=Thermostat 6=Area 7=Message
        # relative_direction: +1=next after num, -1=prev before num, 0=exact
        for type_byte, type_name in [(1, "Zone"), (2, "Unit"), (5, "Thermostat"), (6, "Area")]:
            await _request_or_warn(
                conn,
                f"UploadNames[type={type_name}, after=0, dir=+1]",
                OmniLinkMessageType.UploadNames,
                payload=bytes([type_byte, 0, 0, 1]),
                expected_opcode=int(OmniLinkMessageType.NameData),
            )

    print("\n--- recon complete, session closed cleanly ---")
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
