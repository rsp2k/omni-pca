"""V1 (legacy) Omni-Link protocol over UDP.

The v2 path in :mod:`omni_pca` (TCP, OmniLink2Message, StartChar 0x21,
parameterised RequestProperties / RequestExtendedStatus) is what most
modern firmware speaks. This subpackage exists because some panels are
configured at the network module to listen on **UDP only**, in which case
PC Access falls back to the v1 wire protocol (typed RequestZoneStatus,
RequestUnitStatus, etc., StartChar 0x5A, OmniLinkMessage outer = 0x10).

Reference: clsOmniLinkConnection.cs:353-360 (ConnectionProtocol() returns
V1 for Modem/UDP/Serial, V2 only for TCP).
"""

from __future__ import annotations

from .adapter import OmniClientV1Adapter
from .client import OmniClientV1, OmniNakError, OmniProtocolError
from .connection import (
    HandshakeError,
    InvalidEncryptionKeyError,
    OmniConnectionV1,
    RequestTimeoutError,
)
from .messages import (
    NameRecord,
    NameType,
    parse_v1_aux_status,
    parse_v1_namedata,
    parse_v1_system_status,
    parse_v1_thermostat_status,
    parse_v1_unit_status,
    parse_v1_zone_status,
)

__all__ = [
    "HandshakeError",
    "InvalidEncryptionKeyError",
    "NameRecord",
    "NameType",
    "OmniClientV1",
    "OmniClientV1Adapter",
    "OmniConnectionV1",
    "OmniNakError",
    "OmniProtocolError",
    "RequestTimeoutError",
    "parse_v1_aux_status",
    "parse_v1_namedata",
    "parse_v1_system_status",
    "parse_v1_thermostat_status",
    "parse_v1_unit_status",
    "parse_v1_zone_status",
]
