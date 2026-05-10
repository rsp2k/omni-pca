"""Command-line entry point for ``omni-pca``.

Subcommands:
    decode-pca <file> [--key HEX] [--include-pii] [--field NAME]
    mock-panel  [--host H] [--port P] [--controller-key HEX]
                [--zones-file PATH] [--seed-with-our-house FILE]
    version

The default ``decode-pca`` output is **redacted**: account name, address,
phone, codes and remarks never reach stdout unless the user passes
``--include-pii``. ``--field`` extracts a single value (host, port,
controller_key) for shell scripting.

``mock-panel`` runs a local Omni-Link II controller simulator until
SIGINT — useful for driving the in-progress async client without poking
real hardware.

References:
    pca_file.py — decryption + parsing
    mock_panel.py — controller-side TCP simulator
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from . import __version__
from .mock_panel import MockPanel, MockState
from .pca_file import KEY_EXPORT, KEY_PC01, PcaAccount, parse_pca_file

_DEFAULT_CONTROLLER_KEY_HEX = "00112233445566778899aabbccddeeff"
_DEFAULT_MOCK_PORT = 14369

_ALLOWED_FIELDS = ("host", "port", "controller_key")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="omni-pca",
        description="HAI/Leviton Omni-Link II tooling.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pd = sub.add_parser("decode-pca", help="Decrypt and summarize a .pca file.")
    pd.add_argument("file", type=Path, help="Path to the encrypted .pca file.")
    pd.add_argument(
        "--key",
        type=lambda s: int(s, 0),
        help=(
            "32-bit decryption key (e.g. 0xC1A280B2). "
            "If omitted, tries KEY_EXPORT then KEY_PC01."
        ),
    )
    pd.add_argument(
        "--include-pii",
        action="store_true",
        help="Print account name/address/phone/code (PII).",
    )
    pd.add_argument(
        "--field",
        choices=_ALLOWED_FIELDS,
        help="Print only one field for scripting (host, port, controller_key).",
    )

    pm = sub.add_parser(
        "mock-panel",
        help="Run a local Omni-Link II controller simulator (test harness).",
    )
    pm.add_argument("--host", default="127.0.0.1", help="Bind host (default 127.0.0.1).")
    pm.add_argument(
        "--port",
        type=int,
        default=_DEFAULT_MOCK_PORT,
        help=f"Bind port (default {_DEFAULT_MOCK_PORT}).",
    )
    pm.add_argument(
        "--controller-key",
        default=_DEFAULT_CONTROLLER_KEY_HEX,
        help="32 hex chars (16 bytes) for the panel ControllerKey.",
    )
    pm.add_argument(
        "--zones-file",
        type=Path,
        help="Plain text file: one 'INDEX NAME' per line, seeds MockState.zones.",
    )
    pm.add_argument(
        "--seed-with-our-house",
        type=Path,
        help="Path to a .pca file; its zones/units/areas seed MockState.",
    )
    pm.add_argument(
        "--debug", action="store_true", help="Verbose mock-panel debug logging."
    )

    sub.add_parser("version", help="Print package version and exit.")
    return p


def _try_decode(file: Path, key: int | None) -> tuple[int, PcaAccount]:
    keys = [key] if key is not None else [KEY_EXPORT, KEY_PC01]
    last_exc: Exception | None = None
    for k in keys:
        try:
            account = parse_pca_file(file, key=k)
        except (ValueError, EOFError, OSError) as exc:
            last_exc = exc
            continue
        # Sanity: the version_tag should start with 'PCA' for a valid decode.
        if account.version_tag.startswith("PCA"):
            return k, account
    if last_exc is not None:
        raise last_exc
    raise ValueError(f"no key produced a valid PCA header for {file}")


def _print_summary(account: PcaAccount, include_pii: bool) -> None:
    print(f"version_tag        = {account.version_tag}")
    print(f"file_version       = {account.file_version}")
    print(
        f"firmware           = {account.firmware_major}."
        f"{account.firmware_minor} r{account.firmware_revision} (model={account.model})"
    )
    if account.network_address is not None:
        print(f"network_address    = {account.network_address}")
        print(f"network_port       = {account.network_port}")
    if account.controller_key is not None:
        print(f"controller_key     = {account.controller_key.hex()}")
    if include_pii:
        print(f"account_name       = {account.account_name!r}")
        print(f"account_address    = {account.account_address!r}")
        print(f"account_phone      = {account.account_phone!r}")
        print(f"account_code       = {account.account_code!r}")
    else:
        print("(PII fields redacted; pass --include-pii to display)")


def _print_field(account: PcaAccount, field: str) -> int:
    if field == "host":
        if account.network_address is None:
            print("error: no network_address found", file=sys.stderr)
            return 2
        print(account.network_address)
    elif field == "port":
        if account.network_port is None:
            print("error: no network_port found", file=sys.stderr)
            return 2
        print(account.network_port)
    elif field == "controller_key":
        if account.controller_key is None:
            print("error: no controller_key found", file=sys.stderr)
            return 2
        print(account.controller_key.hex())
    else:  # pragma: no cover -- argparse already restricts choices
        print(f"unknown field {field}", file=sys.stderr)
        return 2
    return 0


def _parse_zones_file(path: Path) -> dict[int, str]:
    """Read 'INDEX NAME' lines into a {idx: name} dict; '#' starts a comment."""
    out: dict[int, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        idx_str, _, name = line.partition(" ")
        try:
            idx = int(idx_str)
        except ValueError as exc:
            raise ValueError(
                f"{path}: cannot parse index from {raw!r} — expected 'INDEX NAME'"
            ) from exc
        out[idx] = name.strip()
    return out


def _build_mock_state(args: argparse.Namespace) -> MockState:
    state = MockState()
    if args.seed_with_our_house is not None:
        # The .pca header already gives us model + firmware; zone/unit/area
        # name extraction from the body isn't wired up in pca_file yet.
        _, account = _try_decode(args.seed_with_our_house, None)
        state.model_byte = account.model
        state.firmware_major = account.firmware_major
        state.firmware_minor = account.firmware_minor
        state.firmware_revision = account.firmware_revision
        print(
            f"# seeded model={account.model} fw={account.firmware_major}."
            f"{account.firmware_minor}.{account.firmware_revision}",
            file=sys.stderr,
        )
    if args.zones_file is not None:
        state.zones = _parse_zones_file(args.zones_file)
        print(f"# loaded {len(state.zones)} zone names", file=sys.stderr)
    return state


async def _run_mock_panel(args: argparse.Namespace) -> int:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    try:
        controller_key = bytes.fromhex(args.controller_key)
    except ValueError as exc:
        print(f"error: --controller-key not valid hex: {exc}", file=sys.stderr)
        return 2
    if len(controller_key) != 16:
        print(
            f"error: --controller-key must be 16 bytes (32 hex chars), got {len(controller_key)}",
            file=sys.stderr,
        )
        return 2
    state = _build_mock_state(args)
    panel = MockPanel(controller_key=controller_key, state=state)
    async with panel.serve(host=args.host, port=args.port) as (host, port):
        print(f"omni-pca mock-panel listening on {host}:{port}")
        print("# Ctrl-C to stop", file=sys.stderr)
        with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
            await asyncio.Event().wait()  # block until cancelled
    print(f"# served {panel.session_count} session(s)", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "version":
        print(__version__)
        return 0
    if args.cmd == "decode-pca":
        used_key, account = _try_decode(args.file, args.key)
        if args.field is not None:
            return _print_field(account, args.field)
        print(f"# decoded with key=0x{used_key:08X}", file=sys.stderr)
        _print_summary(account, include_pii=args.include_pii)
        return 0
    if args.cmd == "mock-panel":
        try:
            return asyncio.run(_run_mock_panel(args))
        except KeyboardInterrupt:
            return 0
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
