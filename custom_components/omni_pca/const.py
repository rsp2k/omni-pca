"""Constants for the HAI/Leviton Omni Panel integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final

DOMAIN: Final = "omni_pca"

DEFAULT_PORT: Final = 4369
DEFAULT_TIMEOUT: Final = 5.0

CONF_CONTROLLER_KEY: Final = "controller_key"

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
