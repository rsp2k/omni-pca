#!/usr/bin/env python3
"""Probe the v1 UploadNames streaming flow.

Sends UploadNames (no payload), then a series of Acknowledge messages,
dumping each reply until we get an EOD or 30 records (whichever comes
first). Confirms the lock-step pattern PC Access uses for bulk reads.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from probe_v1 import _load_key  # type: ignore  # noqa: E402

from omni_pca.opcodes import OmniLinkMessageType
from omni_pca.v1 import OmniConnectionV1


_NAME_TYPE_LABELS = {
    1: "Zone", 2: "Unit", 3: "Button", 4: "Code",
    5: "Thermostat", 6: "Area", 7: "Message",
}


def _decode_namedata(payload: bytes) -> str:
    """Best-effort decode of a NameData payload for display."""
    if len(payload) < 3:
        return f"<short payload: {payload.hex()}>"
    name_type = payload[0]
    # Heuristic: zones/messages are 15-char names, others 12. With one-byte
    # NameNumber, payload length = 1 (type) + 1 (num) + L (name) + 1 (term).
    # With two-byte NameNumber: 1 + 2 + L + 1.
    L_15 = 15 + 3  # one-byte form, 15-char name
    L_12 = 12 + 3  # one-byte form, 12-char name
    if len(payload) == L_15 or len(payload) == L_15 + 1:
        # 15-char name (Zone or Message), one-byte num.
        num = payload[1]
        name = payload[2:2 + 15].rstrip(b"\x00").decode("utf-8", errors="replace")
    elif len(payload) == L_12 or len(payload) == L_12 + 1:
        # 12-char name, one-byte num.
        num = payload[1]
        name = payload[2:2 + 12].rstrip(b"\x00").decode("utf-8", errors="replace")
    else:
        # Two-byte num form (NameNumber > 255): payload[1..2] = BE u16, then name.
        num = (payload[1] << 8) | payload[2]
        name = payload[3:].rstrip(b"\x00").decode("utf-8", errors="replace")

    label = _NAME_TYPE_LABELS.get(name_type, f"type{name_type}")
    return f"{label} #{num}: {name!r}"


async def amain(args: argparse.Namespace) -> int:
    key = _load_key(args.key)
    print(f"[stream probe] target {args.host}:{args.port}\n")

    async with OmniConnectionV1(
        host=args.host, port=args.port, controller_key=key, timeout=4.0
    ) as conn:
        from omni_pca.message import Message, START_CHAR_V1_UNADDRESSED

        # Step 1: bare UploadNames.
        upload = Message(
            start_char=START_CHAR_V1_UNADDRESSED,
            data=bytes([int(OmniLinkMessageType.UploadNames)]),
        )
        seq, fut = conn._send_encrypted(upload)
        reply = conn._decode_inner(await fut)
        print(f"reply 1 (seq={seq}): opcode={reply.opcode}  {_decode_namedata(reply.payload) if reply.opcode == int(OmniLinkMessageType.NameData) else f'(payload={reply.payload.hex()!r})'}")

        if reply.opcode != int(OmniLinkMessageType.NameData):
            print("panel didn't reply with NameData — streaming flow may not apply here")
            return 0

        # Step 2..N: Acknowledge → next NameData (or EOD).
        for i in range(2, args.max + 1):
            ack = Message(
                start_char=START_CHAR_V1_UNADDRESSED,
                data=bytes([int(OmniLinkMessageType.Ack)]),
            )
            seq, fut = conn._send_encrypted(ack)
            reply = conn._decode_inner(await fut)

            if reply.opcode == int(OmniLinkMessageType.EOD):
                print(f"reply {i} (seq={seq}): EOD — end of stream after {i-1} records")
                return 0
            if reply.opcode == int(OmniLinkMessageType.NameData):
                print(f"reply {i} (seq={seq}): {_decode_namedata(reply.payload)}")
            else:
                print(f"reply {i} (seq={seq}): unexpected opcode {reply.opcode}, "
                      f"payload={reply.payload.hex()}")
                return 1

        print(f"\nstopped after {args.max} replies (no EOD seen)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.1.9")
    parser.add_argument("--port", type=int, default=4369)
    parser.add_argument("--key", help="32 hex chars; overrides env/.omni_key")
    parser.add_argument("--max", type=int, default=20, help="stop after N replies")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
