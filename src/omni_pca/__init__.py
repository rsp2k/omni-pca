"""omni-pca — async client for HAI/Leviton Omni-Link II panels."""

from importlib.metadata import PackageNotFoundError, version

from .programs import (
    Days,
    Program,
    ProgramCond,
    ProgramType,
    decode_program_table,
    iter_defined,
)

try:
    __version__ = version("omni-pca")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "Days",
    "Program",
    "ProgramCond",
    "ProgramType",
    "__version__",
    "decode_program_table",
    "iter_defined",
]
