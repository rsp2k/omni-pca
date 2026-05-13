"""Constants for the HAI/Leviton Omni Panel integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

DOMAIN: Final = "omni_pca"

DEFAULT_PORT: Final = 4369
DEFAULT_TIMEOUT: Final = 5.0

CONF_CONTROLLER_KEY: Final = "controller_key"
CONF_TRANSPORT: Final = "transport"

# Optional: when set, load panel programs from a .pca file at this path
# instead of enumerating them over the wire on every entry refresh. The
# .pca file is decrypted with CONF_PCA_KEY (the per-install key from
# PCA01.CFG, or 0 for a plain-text dump). Both must be set together.
CONF_PCA_PATH: Final = "pca_path"
CONF_PCA_KEY: Final = "pca_key"

TRANSPORT_TCP: Final = "tcp"
TRANSPORT_UDP: Final = "udp"
DEFAULT_TRANSPORT: Final = TRANSPORT_TCP

MANUFACTURER: Final = "HAI / Leviton"

# Polling interval. Most state arrives via unsolicited push messages, so
# this is just a safety net that keeps `last_update_success` honest if the
# panel goes quiet.
SCAN_INTERVAL: Final = timedelta(seconds=30)

# Background event-listener task name, surfaced to ``asyncio.all_tasks()``
# for diagnostics.
EVENT_TASK_NAME: Final = "omni_pca-event-listener"

# Upper bound for the discovery walk. The protocol caps object indices at
# uint16, but Omni panels never approach that — most installs have <100
# zones / units / areas, so we stop early when discovery returns EOD.
MAX_OBJECT_INDEX: Final = 0xFFFF

# Length, in characters, of a hex-encoded 16-byte controller key.
CONTROLLER_KEY_HEX_LEN: Final = 32

LOGGER: Final = logging.getLogger(__package__)
