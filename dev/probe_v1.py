#!/usr/bin/env python3
"""Phase-1 smoke test: v1-over-UDP handshake + RequestSystemInformation.

Run inside the project venv:
    cd /home/kdm/home-auto/omni-pca
    uv run python dev/probe_v1.py [--host 192.168.1.9] [--port 4369]

Requires the panel's controller key. Picks it up from (in order):
  1. ``--key 32hex`` on the command line
  2. ``OMNI_KEY`` env var
  3. ``dev/.omni_key`` file (gitignored)
  4. The bundled ``.pca`` plain fixture (developer-only fallback)

Success criteria: panel returns a v1 SystemInformation message (opcode 18)
within the timeout. Failure modes we want to distinguish:
  * UDP socket fails to open  → routing / firewall
  * Handshake step 2 timeout → wrong port, wrong panel
  * Handshake step 4 termination → wrong controller key
  * SystemInformation timeout → v1 path isn't doing what we think
  * SystemInformation reply  → v1-over-UDP is real, proceed to Phase 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from omni_pca.v1.connection import (
    HandshakeError,
    InvalidEncryptionKeyError,
    OmniConnectionV1,
    RequestTimeoutError,
)
from omni_pca.opcodes import OmniLinkMessageType


def _load_key(arg_key: str | None) -> bytes:
    if arg_key:
        return bytes.fromhex(arg_key)
    env = os.environ.get("OMNI_KEY")
    if env:
        return bytes.fromhex(env)
    keyfile = Path(__file__).parent / ".omni_key"
    if keyfile.exists():
        return bytes.fromhex(keyfile.read_text().strip())
    fixture = Path("/home/kdm/home-auto/HAI/pca-re/extracted/Our_House.pca.plain")
    if fixture.exists():
        from omni_pca.pca_file import (
            PcaReader,
            _CAP_OMNI_PRO_II,
            _parse_header,
            _walk_to_connection,
        )

        r = PcaReader(fixture.read_bytes())
        _parse_header(r)
        _walk_to_connection(r, _CAP_OMNI_PRO_II)
        r.string8_fixed(120)  # network_address
        r.string8_fixed(5)    # port
        return bytes.fromhex(r.string8_fixed(32).ljust(32, "0")[:32])
    raise SystemExit("no controller key: pass --key, set OMNI_KEY, or create dev/.omni_key")


def _decode_system_information(payload: bytes) -> dict[str, object]:
    """Parse the v1 SystemInformation payload (mirrors clsOLMsgSystemInformation)."""
    if len(payload) < 29:
        raise ValueError(f"SystemInformation payload too short: {len(payload)} bytes")
    return {
        "opcode": payload[0],
        "model": payload[1],
        "fw_major": payload[2],
        "fw_minor": payload[3],
        "fw_revision": int.from_bytes(payload[4:5], "big", signed=True),
        "local_phone": payload[5:29].rstrip(b"\x00").decode("ascii", errors="replace"),
    }


async def amain(args: argparse.Namespace) -> int:
    key = _load_key(args.key)
    print(f"[probe] target {args.host}:{args.port}  key=...{key[-2:].hex()} (16 B)")

    try:
        async with OmniConnectionV1(
            host=args.host,
            port=args.port,
            controller_key=key,
            timeout=args.timeout,
            retry_count=args.retries,
        ) as conn:
            print(f"[probe] handshake OK  state={conn.state.name}  "
                  f"session_key=...{conn.session_key[-2:].hex() if conn.session_key else 'n/a'}")

            print("[probe] sending v1 RequestSystemInformation (opcode 17)")
            reply = await conn.request(OmniLinkMessageType.RequestSystemInformation)
            print(f"[probe] reply: start_char={reply.start_char:#04x}  "
                  f"opcode={reply.opcode}  payload={reply.data.hex()}")

            if reply.opcode != int(OmniLinkMessageType.SystemInformation):
                print(f"[probe] WARNING: expected opcode 18 (SystemInformation), "
                      f"got {reply.opcode}")
                return 2

            info = _decode_system_information(reply.data)
            print(f"[probe] ✓ v1-over-UDP works")
            print(f"        model      = {info['model']}")
            print(f"        firmware   = {info['fw_major']}.{info['fw_minor']}.{info['fw_revision']}")
            print(f"        phone      = {info['local_phone']!r}")

    except InvalidEncryptionKeyError as exc:
        print(f"[probe] handshake terminated: wrong controller key? ({exc})")
        return 1
    except HandshakeError as exc:
        print(f"[probe] handshake failed: {exc}")
        return 1
    except RequestTimeoutError as exc:
        print(f"[probe] no reply to RequestSystemInformation: {exc}")
        print("        → handshake worked but v1 path isn't responding. "
              "Check tcpdump for what's on the wire.")
        return 2
    except OSError as exc:
        print(f"[probe] socket error: {exc}")
        return 1

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.9")
    parser.add_argument("--port", type=int, default=4369)
    parser.add_argument("--key", help="32 hex chars; overrides env/.omni_key")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--debug", action="store_true",
                        help="enable DEBUG logging (TX/RX packet dump)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
