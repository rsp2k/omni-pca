"""Command-line entry point for ``omni-pca``.

Subcommands:
    decode-pca <file> [--key HEX] [--include-pii] [--field NAME]
    version

The default ``decode-pca`` output is **redacted**: account name, address,
phone, codes and remarks never reach stdout unless the user passes
``--include-pii``. ``--field`` extracts a single value (host, port,
controller_key) for shell scripting.

References:
    pca_file.py — decryption + parsing
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .pca_file import KEY_EXPORT, KEY_PC01, PcaAccount, parse_pca_file

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
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
