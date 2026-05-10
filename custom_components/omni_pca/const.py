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

# Length, in characters, of a hex-encoded 16-byte controller key.
CONTROLLER_KEY_HEX_LEN: Final = 32

LOGGER: Final = logging.getLogger(__package__)
