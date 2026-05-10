"""DataUpdateCoordinator that owns the long-lived OmniClient connection.

The coordinator caches *static* panel topology (system info, zone names,
unit names, area names) on first refresh and only re-queries dynamic state
on subsequent updates. Unsolicited messages from the panel are also routed
through here so binary sensors flip immediately without waiting for the
next 30s poll.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from omni_pca.client import ObjectType, OmniClient
from omni_pca.connection import (
    ConnectionError as OmniConnectionError,
)
from omni_pca.connection import (
    HandshakeError,
    InvalidEncryptionKeyError,
    RequestTimeoutError,
)
from omni_pca.models import SystemInformation, SystemStatus, ZoneProperties

from .const import DOMAIN, LOGGER, MANUFACTURER, SCAN_INTERVAL

if TYPE_CHECKING:
    from omni_pca.message import Message


@dataclass(slots=True)
class OmniZoneState:
    """Per-zone state combining static name with dynamic status."""

    index: int
    name: str
    zone_type: int
    area: int
    status: int  # raw zone status byte from the panel
    loop: int

    @property
    def is_open(self) -> bool:
        """True when the zone is tripped / not-ready / open.

        The Omni-Link II ``ZoneStatus`` byte packs current condition in the
        low nibble. 0 = secure (closed). Any non-zero current condition is
        treated as "not secure" for binary-sensor purposes.
        """
        return (self.status & 0x03) != 0


@dataclass(slots=True)
class OmniData:
    """Top-level coordinator data exposed to entities."""

    system_information: SystemInformation
    system_status: SystemStatus | None
    zones: dict[int, OmniZoneState]
    unit_names: dict[int, str] = field(default_factory=dict)
    area_names: dict[int, str] = field(default_factory=dict)


class OmniDataUpdateCoordinator(DataUpdateCoordinator[OmniData]):
    """Coordinator that owns one OmniClient and one panel device."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        host: str,
        port: int,
        controller_key: bytes,
    ) -> None:
        super().__init__(
            hass,
            LOGGER,
            name=f"{DOMAIN} {host}:{port}",
            update_interval=SCAN_INTERVAL,
            config_entry=entry,
        )
        self._host = host
        self._port = port
        self._controller_key = controller_key
        self._client: OmniClient | None = None
        self._static_loaded = False
        self._zone_names: dict[int, str] = {}
        self._unit_names: dict[int, str] = {}
        self._area_names: dict[int, str] = {}
        self._system_information: SystemInformation | None = None

    # ---- public surface --------------------------------------------------

    @property
    def unique_id(self) -> str:
        """Stable identifier for this panel (host:port)."""
        return f"{self._host}:{self._port}"

    @property
    def device_info(self) -> DeviceInfo:
        """DeviceInfo for the single hub device this coordinator represents."""
        info = self._system_information
        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            name="Omni Pro II",
            manufacturer=MANUFACTURER,
            model=info.model_name if info is not None else None,
            sw_version=info.firmware_version if info is not None else None,
            configuration_url=None,
        )

    async def async_shutdown(self) -> None:
        """Tear down the client connection on unload."""
        if self._client is not None:
            client = self._client
            self._client = None
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                LOGGER.debug("error closing OmniClient", exc_info=True)
        await super().async_shutdown()

    # ---- DataUpdateCoordinator hook -------------------------------------

    async def _async_update_data(self) -> OmniData:
        try:
            client = await self._ensure_connected()
            if not self._static_loaded:
                await self._load_static(client)
            system_status = await self._safe_system_status(client)
            zones = await self._snapshot_zones(client)
        except (InvalidEncryptionKeyError, HandshakeError) as err:
            # Surface as auth failure so HA triggers the reauth flow.
            from homeassistant.exceptions import ConfigEntryAuthFailed

            raise ConfigEntryAuthFailed(str(err)) from err
        except (OmniConnectionError, RequestTimeoutError, OSError) as err:
            await self._drop_client()
            raise UpdateFailed(f"panel unreachable: {err}") from err

        assert self._system_information is not None  # set by _load_static
        return OmniData(
            system_information=self._system_information,
            system_status=system_status,
            zones=zones,
            unit_names=dict(self._unit_names),
            area_names=dict(self._area_names),
        )

    # ---- internals -------------------------------------------------------

    async def _ensure_connected(self) -> OmniClient:
        if self._client is not None:
            return self._client
        client = OmniClient(
            self._host,
            port=self._port,
            controller_key=self._controller_key,
        )
        # Manually drive __aenter__ so we can keep the connection open
        # across update cycles instead of using `async with`.
        await client.__aenter__()
        try:
            await client.subscribe(self._handle_unsolicited)
        except Exception:
            await client.__aexit__(None, None, None)
            raise
        self._client = client
        return client

    async def _drop_client(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.__aexit__(None, None, None)
        except Exception:
            LOGGER.debug("error during reconnect cleanup", exc_info=True)

    async def _load_static(self, client: OmniClient) -> None:
        self._system_information = await client.get_system_information()
        self._zone_names = await client.list_zone_names()
        # Unit / area names are best-effort; some panels may not have any.
        try:
            self._unit_names = await client.list_unit_names()
        except Exception:
            LOGGER.debug("list_unit_names failed; continuing", exc_info=True)
            self._unit_names = {}
        try:
            self._area_names = await client.list_area_names()
        except Exception:
            LOGGER.debug("list_area_names failed; continuing", exc_info=True)
            self._area_names = {}
        self._static_loaded = True
        LOGGER.debug(
            "loaded static topology: %d zones, %d units, %d areas",
            len(self._zone_names),
            len(self._unit_names),
            len(self._area_names),
        )

    async def _safe_system_status(self, client: OmniClient) -> SystemStatus | None:
        try:
            return await client.get_system_status()
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("get_system_status failed; continuing", exc_info=True)
            return None

    async def _snapshot_zones(self, client: OmniClient) -> dict[int, OmniZoneState]:
        zones: dict[int, OmniZoneState] = {}
        for index, name in self._zone_names.items():
            try:
                props = await client.get_object_properties(ObjectType.ZONE, index)
            except (OmniConnectionError, RequestTimeoutError):
                raise
            except Exception:
                LOGGER.debug("zone %d snapshot failed; skipping", index, exc_info=True)
                continue
            if not isinstance(props, ZoneProperties):
                continue
            zones[index] = OmniZoneState(
                index=index,
                name=name,
                zone_type=props.zone_type,
                area=props.area,
                status=props.status,
                loop=props.loop,
            )
        return zones

    async def _handle_unsolicited(self, msg: Message) -> None:
        """Push-driven update path.

        We don't try to be clever about parsing every unsolicited opcode
        here. The simplest correct behavior is to nudge HA to refetch on
        any panel-initiated message; entities will see fresh zone state
        within one round-trip.
        """
        LOGGER.debug("unsolicited opcode %#04x payload=%s", msg.opcode, msg.payload.hex())
        # Schedule a refresh on the event loop without awaiting from the
        # subscriber callback (which lives in the connection's read loop).
        self.hass.async_create_task(self._refresh_after_push())

    async def _refresh_after_push(self) -> None:
        if self.data is None or self._client is None:
            return
        try:
            zones = await self._snapshot_zones(self._client)
        except (OmniConnectionError, RequestTimeoutError):
            await self.async_request_refresh()
            return
        # Mutate a copy so listeners see a brand-new object identity.
        new_data = replace(self.data, zones=zones)
        self.async_set_updated_data(new_data)
