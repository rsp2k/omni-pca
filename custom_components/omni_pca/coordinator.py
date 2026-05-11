"""DataUpdateCoordinator that owns the long-lived OmniClient connection.

Lifecycle
---------
1. ``async_config_entry_first_refresh`` connects, runs a one-time
   *discovery* pass that enumerates every named zone / unit / area /
   thermostat / button / program on the panel, and seeds ``self.data``
   with a populated :class:`OmniData`.
2. ``_async_update_data`` is then called every :data:`SCAN_INTERVAL` to
   re-poll *live state only* (extended status for zones / units /
   thermostats, basic status for areas).
3. A background task (:meth:`_run_event_listener`) consumes
   :meth:`OmniClient.events` for the lifetime of the entry; whenever a
   typed :class:`SystemEvent` arrives, the relevant slice of state is
   patched in-place and ``async_set_updated_data`` fires so HA pushes
   updates to subscribed entities without waiting for the next poll.

The library's :class:`OmniClient` is the *only* thing that talks to the
wire. We keep one client per coordinator and close it on shutdown; on a
recoverable :class:`OmniConnectionError` we drop and recreate it on the
next refresh, preserving the existing :class:`OmniData` so entities don't
flicker to "unavailable" between attempts.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field, replace

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from omni_pca.client import ObjectType as ClientObjectType
from omni_pca.client import OmniClient
from omni_pca.connection import (
    ConnectionError as OmniConnectionError,
)
from omni_pca.connection import (
    HandshakeError,
    InvalidEncryptionKeyError,
    RequestTimeoutError,
)
from omni_pca.events import (
    AcLost,
    AcRestored,
    AlarmActivated,
    AlarmCleared,
    ArmingChanged,
    BatteryLow,
    BatteryRestored,
    SystemEvent,
    UnitStateChanged,
    ZoneStateChanged,
)
from omni_pca.models import (
    OBJECT_TYPE_TO_PROPERTIES,
    AreaProperties,
    AreaStatus,
    ButtonProperties,
    ObjectType,
    ProgramProperties,
    SystemInformation,
    SystemStatus,
    ThermostatProperties,
    ThermostatStatus,
    UnitProperties,
    UnitStatus,
    ZoneProperties,
    ZoneStatus,
)
from omni_pca.opcodes import OmniLink2MessageType

from .const import (
    DOMAIN,
    EVENT_TASK_NAME,
    LOGGER,
    MANUFACTURER,
    MAX_OBJECT_INDEX,
    SCAN_INTERVAL,
)

# --------------------------------------------------------------------------
# Public data shape exposed to entities
# --------------------------------------------------------------------------


@dataclass(slots=True)
class OmniData:
    """Snapshot of everything a coordinator's entities can read.

    Discovery dictionaries (``zones``, ``units``, ``areas``,
    ``thermostats``, ``buttons``, ``programs``) are populated once on
    first refresh and never re-walked — they describe panel topology,
    which only changes when the installer reprograms the controller and
    the user reloads the integration.

    Live ``*_status`` dictionaries are re-populated on every poll *and*
    patched in-place from the event listener.
    """

    system_info: SystemInformation
    zones: dict[int, ZoneProperties] = field(default_factory=dict)
    units: dict[int, UnitProperties] = field(default_factory=dict)
    areas: dict[int, AreaProperties] = field(default_factory=dict)
    thermostats: dict[int, ThermostatProperties] = field(default_factory=dict)
    buttons: dict[int, ButtonProperties] = field(default_factory=dict)
    programs: dict[int, ProgramProperties] = field(default_factory=dict)

    zone_status: dict[int, ZoneStatus] = field(default_factory=dict)
    unit_status: dict[int, UnitStatus] = field(default_factory=dict)
    area_status: dict[int, AreaStatus] = field(default_factory=dict)
    thermostat_status: dict[int, ThermostatStatus] = field(default_factory=dict)

    system_status: SystemStatus | None = None
    last_event: SystemEvent | None = None


# --------------------------------------------------------------------------
# Coordinator
# --------------------------------------------------------------------------


class OmniDataUpdateCoordinator(DataUpdateCoordinator[OmniData]):
    """Coordinator that owns one :class:`OmniClient` and one panel device."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        host: str,
        port: int,
        controller_key: bytes,
        transport: str = "tcp",
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
        self._transport = transport
        self._client: OmniClient | None = None
        self._discovery_done = False
        self._discovered: OmniData | None = None
        self._event_task: asyncio.Task[None] | None = None

    # ---- public surface --------------------------------------------------

    @property
    def unique_id(self) -> str:
        """Stable identifier for this panel (host:port)."""
        return f"{self._host}:{self._port}"

    @property
    def client(self) -> OmniClient:
        """The live OmniClient. Raises if the coordinator hasn't connected yet."""
        if self._client is None:
            raise RuntimeError("OmniClient is not connected")
        return self._client

    @property
    def device_info(self) -> DeviceInfo:
        """DeviceInfo for the single hub device this coordinator represents."""
        info = self._discovered.system_info if self._discovered is not None else None
        return DeviceInfo(
            identifiers={(DOMAIN, self.unique_id)},
            name=info.model_name if info is not None else "Omni Panel",
            manufacturer=MANUFACTURER,
            model=info.model_name if info is not None else None,
            sw_version=info.firmware_version if info is not None else None,
            configuration_url=None,
        )

    async def async_shutdown(self) -> None:
        """Tear down the event task and the client connection on unload."""
        await self._cancel_event_task()
        await self._drop_client()
        await super().async_shutdown()

    # ---- DataUpdateCoordinator hook -------------------------------------

    async def _async_update_data(self) -> OmniData:
        try:
            client = await self._ensure_connected()
            if not self._discovery_done:
                self._discovered = await self._run_discovery(client)
                self._discovery_done = True
                self._start_event_task()

            assert self._discovered is not None
            base = self._discovered
            zone_status = await self._poll_zone_status(client, base.zones)
            unit_status = await self._poll_unit_status(client, base.units)
            area_status = await self._poll_area_status(client, base.areas)
            thermostat_status = await self._poll_thermostat_status(
                client, base.thermostats
            )
            system_status = await self._safe_system_status(client)
        except (InvalidEncryptionKeyError, HandshakeError) as err:
            # Surface as auth failure so HA triggers the reauth flow.
            await self._drop_client()
            raise ConfigEntryAuthFailed(str(err)) from err
        except (OmniConnectionError, RequestTimeoutError, OSError) as err:
            await self._drop_client()
            raise UpdateFailed(f"panel unreachable: {err}") from err

        # Preserve any last_event already captured by the event task; the
        # poll path doesn't see push events so it must not overwrite it.
        last_event = self.data.last_event if self.data is not None else None

        return replace(
            self._discovered,
            zone_status=zone_status,
            unit_status=unit_status,
            area_status=area_status,
            thermostat_status=thermostat_status,
            system_status=system_status,
            last_event=last_event,
        )

    # ---- connection management ------------------------------------------

    async def _ensure_connected(self) -> OmniClient:
        if self._client is not None:
            return self._client
        if self._transport == "udp":
            # Panels listening UDP-only speak the v1 wire protocol, not
            # v2. The adapter exposes the OmniClient API surface this
            # coordinator was written against, but underneath it drives
            # an OmniConnectionV1 + the typed v1 status/command opcodes.
            from omni_pca.v1 import OmniClientV1Adapter

            client: OmniClient = OmniClientV1Adapter(  # type: ignore[assignment]
                self._host,
                port=self._port,
                controller_key=self._controller_key,
            )
        else:
            client = OmniClient(
                self._host,
                port=self._port,
                controller_key=self._controller_key,
                transport=self._transport,  # type: ignore[arg-type]
            )
        # Drive __aenter__ manually so the client survives across update
        # cycles; we close it explicitly on shutdown / failure.
        await client.__aenter__()
        self._client = client
        return client

    async def _drop_client(self) -> None:
        if self._client is None:
            return
        client = self._client
        self._client = None
        try:
            await client.__aexit__(None, None, None)
        except Exception:  # pragma: no cover - best-effort cleanup
            LOGGER.debug("error during client cleanup", exc_info=True)

    # ---- discovery -------------------------------------------------------

    async def _run_discovery(self, client: OmniClient) -> OmniData:
        """Walk every object type once and stash the static topology."""
        system_info = await client.get_system_information()

        zones = await self._discover_zones(client)
        units = await self._discover_units(client)
        areas = await self._discover_areas(client)
        thermostats = await self._discover_thermostats(client)
        buttons = await self._discover_buttons(client)
        programs = await self._discover_programs(client)

        LOGGER.info(
            "omni_pca discovery: %d zones, %d units, %d areas, "
            "%d thermostats, %d buttons, %d programs",
            len(zones),
            len(units),
            len(areas),
            len(thermostats),
            len(buttons),
            len(programs),
        )
        return OmniData(
            system_info=system_info,
            zones=zones,
            units=units,
            areas=areas,
            thermostats=thermostats,
            buttons=buttons,
            programs=programs,
        )

    async def _discover_zones(
        self, client: OmniClient
    ) -> dict[int, ZoneProperties]:
        names = await self._best_effort(client.list_zone_names, default={})
        out: dict[int, ZoneProperties] = {}
        for index in sorted(names):
            try:
                props = await client.get_object_properties(
                    ClientObjectType.ZONE, index
                )
            except (OmniConnectionError, RequestTimeoutError):
                raise
            except Exception:
                LOGGER.debug("zone %d properties fetch failed", index, exc_info=True)
                continue
            if isinstance(props, ZoneProperties):
                out[index] = props
        return out

    async def _discover_units(
        self, client: OmniClient
    ) -> dict[int, UnitProperties]:
        names = await self._best_effort(client.list_unit_names, default={})
        out: dict[int, UnitProperties] = {}
        for index in sorted(names):
            try:
                props = await client.get_object_properties(
                    ClientObjectType.UNIT, index
                )
            except (OmniConnectionError, RequestTimeoutError):
                raise
            except Exception:
                LOGGER.debug("unit %d properties fetch failed", index, exc_info=True)
                continue
            if isinstance(props, UnitProperties):
                out[index] = props
        return out

    async def _discover_areas(
        self, client: OmniClient
    ) -> dict[int, AreaProperties]:
        names = await self._best_effort(client.list_area_names, default={})
        out: dict[int, AreaProperties] = {}
        for index in sorted(names):
            try:
                props = await client.get_object_properties(
                    ClientObjectType.AREA, index
                )
            except (OmniConnectionError, RequestTimeoutError):
                raise
            except Exception:
                LOGGER.debug("area %d properties fetch failed", index, exc_info=True)
                continue
            if isinstance(props, AreaProperties):
                out[index] = props
        return out

    async def _discover_thermostats(
        self, client: OmniClient
    ) -> dict[int, ThermostatProperties]:
        """Walk thermostat properties via the low-level connection.

        The high-level :meth:`OmniClient.get_object_properties` only knows
        zone/unit/area parsers in v1.0 of the library; thermostats are in
        :data:`OBJECT_TYPE_TO_PROPERTIES` on the model side, so we drive
        the wire ourselves and parse with the model's class.
        """
        return await self._walk_properties(
            client, ObjectType.THERMOSTAT, ThermostatProperties
        )

    async def _discover_buttons(
        self, client: OmniClient
    ) -> dict[int, ButtonProperties]:
        return await self._walk_properties(
            client, ObjectType.BUTTON, ButtonProperties
        )

    async def _discover_programs(
        self, client: OmniClient
    ) -> dict[int, ProgramProperties]:
        # Programs aren't reachable via the Properties opcode (the C# side
        # uses a separate request/reply pair), so we just return an empty
        # dict. We keep the field on OmniData so Phase B can plug in real
        # discovery the moment the library exposes it. AMBIGUITY: the spec
        # asks for "named programs" — there's no on-the-wire path for that
        # in v1.0 of omni_pca, so an empty mapping is the honest answer.
        _ = client, ProgramProperties
        return {}

    async def _walk_properties(
        self,
        client: OmniClient,
        object_type: ObjectType,
        parser: type,
    ) -> dict[int, object]:
        """Walk every defined object of ``object_type`` and parse with ``parser``.

        Mirrors the strategy used by ``OmniClient._walk_named_objects`` but
        works for any model in :data:`OBJECT_TYPE_TO_PROPERTIES` (the
        client's internal parser table only covers zones/units/areas in
        v1.0). We drive ``RequestProperties`` directly on the connection
        so we don't have to monkey-patch the library.

        On UDP/v1 panels there is no ``RequestProperties`` opcode at all,
        so we fall back to the v1 adapter's name-stream-based discovery
        (each object's ``Properties`` is synthesized from its name).
        """
        if parser is None or OBJECT_TYPE_TO_PROPERTIES.get(int(object_type)) is None:
            return {}
        if self._transport == "udp":
            return await self._walk_properties_v1(client, object_type)
        out: dict[int, object] = {}
        cursor = 0
        conn = client.connection
        # Manual request/reply loop with relative_direction=1 (=next).
        for _ in range(MAX_OBJECT_INDEX):
            payload = bytes(
                [
                    int(object_type),
                    (cursor >> 8) & 0xFF,
                    cursor & 0xFF,
                    1,        # relative_direction = next
                    0, 0, 0,  # filter1..3
                ]
            )
            try:
                reply = await conn.request(
                    OmniLink2MessageType.RequestProperties, payload
                )
            except RequestTimeoutError:
                break
            if reply.opcode == int(OmniLink2MessageType.EOD):
                break
            if reply.opcode != int(OmniLink2MessageType.Properties):
                break
            try:
                obj = parser.parse(reply.payload)
            except Exception:
                LOGGER.debug(
                    "parse failed for %s past index %d",
                    object_type.name,
                    cursor,
                    exc_info=True,
                )
                break
            # Object name being empty is OK for buttons/programs but the
            # spec says "named only" — we still keep the entry as a
            # candidate; entity setup filters by truthiness.
            index_attr = getattr(obj, "index", None)
            name_attr = getattr(obj, "name", "")
            if index_attr is None:
                break
            if name_attr:
                out[index_attr] = obj
            cursor = index_attr
            if cursor >= MAX_OBJECT_INDEX:
                break
        return out

    async def _walk_properties_v1(
        self, client: OmniClient, object_type: ObjectType
    ) -> dict[int, object]:
        """V1 fallback for :meth:`_walk_properties`.

        v1 has no RequestProperties opcode — names come from streaming
        UploadNames and the rest of the Properties fields can't be
        recovered from the wire. We delegate to the adapter's
        ``get_object_properties`` (which synthesizes a minimal record
        from the cached name list) and skip anything it returns ``None``
        for.
        """
        # Pick the right per-type name lister. The adapter caches the
        # UploadNames stream output so these are nearly free after the
        # first call this discovery pass.
        if object_type == ObjectType.THERMOSTAT:
            names = await client.list_thermostat_names()  # type: ignore[attr-defined]
        elif object_type == ObjectType.BUTTON:
            names = await client.list_button_names()  # type: ignore[attr-defined]
        else:
            # Programs / Messages / etc — nothing to walk.
            return {}
        out: dict[int, object] = {}
        for idx in sorted(names):
            try:
                props = await client.get_object_properties(object_type, idx)
            except Exception:
                LOGGER.debug(
                    "v1 properties synth failed for %s #%d",
                    object_type.name, idx, exc_info=True,
                )
                continue
            if props is not None:
                out[idx] = props
        return out

    @staticmethod
    async def _best_effort(coro_fn, *, default):
        """Call ``coro_fn()`` and swallow non-transport errors, returning ``default``.

        We let :class:`OmniConnectionError` / :class:`RequestTimeoutError`
        propagate so the coordinator can drop the client and reconnect;
        anything else (a parse failure on a particular reply, NAK on a
        feature the panel doesn't support) is downgraded to a debug log.
        """
        try:
            return await coro_fn()
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("best-effort %s failed", coro_fn.__name__, exc_info=True)
            return default

    # ---- live polling ----------------------------------------------------

    async def _poll_zone_status(
        self, client: OmniClient, zones: dict[int, ZoneProperties]
    ) -> dict[int, ZoneStatus]:
        if not zones:
            return {}
        end = max(zones)
        try:
            records = await client.get_extended_status(ObjectType.ZONE, 1, end)
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("zone extended_status poll failed", exc_info=True)
            return self.data.zone_status if self.data is not None else {}
        return {
            r.index: r
            for r in records
            if isinstance(r, ZoneStatus) and r.index in zones
        }

    async def _poll_unit_status(
        self, client: OmniClient, units: dict[int, UnitProperties]
    ) -> dict[int, UnitStatus]:
        if not units:
            return {}
        end = max(units)
        try:
            records = await client.get_extended_status(ObjectType.UNIT, 1, end)
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("unit extended_status poll failed", exc_info=True)
            return self.data.unit_status if self.data is not None else {}
        return {
            r.index: r
            for r in records
            if isinstance(r, UnitStatus) and r.index in units
        }

    async def _poll_area_status(
        self, client: OmniClient, areas: dict[int, AreaProperties]
    ) -> dict[int, AreaStatus]:
        if not areas:
            return {}
        end = max(areas)
        try:
            records = await client.get_object_status(ObjectType.AREA, 1, end)
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("area status poll failed", exc_info=True)
            return self.data.area_status if self.data is not None else {}
        return {
            r.index: r
            for r in records
            if isinstance(r, AreaStatus) and r.index in areas
        }

    async def _poll_thermostat_status(
        self, client: OmniClient, thermostats: dict[int, ThermostatProperties]
    ) -> dict[int, ThermostatStatus]:
        if not thermostats:
            return {}
        end = max(thermostats)
        try:
            records = await client.get_extended_status(
                ObjectType.THERMOSTAT, 1, end
            )
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("thermostat extended_status poll failed", exc_info=True)
            return (
                self.data.thermostat_status if self.data is not None else {}
            )
        return {
            r.index: r
            for r in records
            if isinstance(r, ThermostatStatus) and r.index in thermostats
        }

    async def _safe_system_status(
        self, client: OmniClient
    ) -> SystemStatus | None:
        try:
            return await client.get_system_status()
        except (OmniConnectionError, RequestTimeoutError):
            raise
        except Exception:
            LOGGER.debug("get_system_status failed", exc_info=True)
            return None

    # ---- event listener --------------------------------------------------

    def _start_event_task(self) -> None:
        if self._event_task is not None and not self._event_task.done():
            return
        self._event_task = self.config_entry.async_create_background_task(
            self.hass,
            self._run_event_listener(),
            EVENT_TASK_NAME,
        )

    async def _cancel_event_task(self) -> None:
        if self._event_task is None:
            return
        task = self._event_task
        self._event_task = None
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run_event_listener(self) -> None:
        """Background loop: consume typed events and push state to entities.

        Re-establishes the iterator on each connection cycle. If the
        client gets dropped (transport error during a poll), we exit; the
        next ``_async_update_data`` will reconnect and respawn this task.
        """
        client = self._client
        if client is None:
            return
        try:
            async for event in client.events():
                self._apply_event(event)
        except asyncio.CancelledError:
            raise
        except (OmniConnectionError, RequestTimeoutError, OSError):
            LOGGER.debug("event listener exited on transport error", exc_info=True)
        except Exception:  # pragma: no cover - defensive
            LOGGER.exception("event listener crashed")

    def _apply_event(self, event: SystemEvent) -> None:
        """Patch ``self.data`` in place for the relevant event subclass."""
        data = self.data
        if data is None:
            return
        new_data = self._patched_for_event(data, event)
        if new_data is not None:
            self.async_set_updated_data(new_data)

    def _patched_for_event(
        self, data: OmniData, event: SystemEvent
    ) -> OmniData | None:
        """Return a new OmniData reflecting ``event``, or ``None`` to skip.

        Pure-ish (mutates only the dict members of the returned snapshot).
        Split out so it stays unit-testable without HA.
        """
        if isinstance(event, ZoneStateChanged):
            existing = data.zone_status.get(event.zone_index)
            if existing is None:
                # We saw a zone the discovery missed — synthesize a record
                # so entities at least see the open/closed flip.
                new_status = ZoneStatus(
                    index=event.zone_index,
                    raw_status=0x01 if event.is_open else 0x00,
                    loop=0,
                )
            else:
                # Toggle low-2-bit current condition; preserve the rest.
                base = existing.raw_status & ~0x03
                new_raw = base | (0x01 if event.is_open else 0x00)
                new_status = ZoneStatus(
                    index=existing.index,
                    raw_status=new_raw,
                    loop=existing.loop,
                )
            patched = dict(data.zone_status)
            patched[event.zone_index] = new_status
            return replace(data, zone_status=patched, last_event=event)

        if isinstance(event, UnitStateChanged):
            existing = data.unit_status.get(event.unit_index)
            new_state = 1 if event.is_on else 0
            if existing is None:
                new_status = UnitStatus(
                    index=event.unit_index,
                    state=new_state,
                    time_remaining_secs=0,
                )
            else:
                # Preserve a brightness level if we have one — the event
                # only carries on/off.
                if existing.state >= 100 and event.is_on:
                    new_status = existing
                else:
                    new_status = UnitStatus(
                        index=existing.index,
                        state=new_state,
                        time_remaining_secs=existing.time_remaining_secs,
                    )
            patched = dict(data.unit_status)
            patched[event.unit_index] = new_status
            return replace(data, unit_status=patched, last_event=event)

        if isinstance(event, ArmingChanged):
            existing = data.area_status.get(event.area_index)
            if existing is None:
                if event.area_index == 0:
                    # System-wide arming change with no specific area —
                    # let the next poll resync.
                    return replace(data, last_event=event)
                new_status = AreaStatus(
                    index=event.area_index,
                    mode=event.new_mode,
                    last_user=event.user_index,
                    entry_timer_secs=0,
                    exit_timer_secs=0,
                    alarms=0,
                )
            else:
                new_status = AreaStatus(
                    index=existing.index,
                    mode=event.new_mode,
                    last_user=event.user_index,
                    entry_timer_secs=existing.entry_timer_secs,
                    exit_timer_secs=existing.exit_timer_secs,
                    alarms=existing.alarms,
                )
            patched = dict(data.area_status)
            patched[new_status.index] = new_status
            return replace(data, area_status=patched, last_event=event)

        if isinstance(event, AlarmActivated | AlarmCleared):
            # Force a poll so AreaStatus.alarms picks up the current bits.
            self.hass.async_create_task(self.async_request_refresh())
            return replace(data, last_event=event)

        if isinstance(event, AcLost | AcRestored | BatteryLow | BatteryRestored):
            # Just stash the event; the system_* binary sensors derive
            # their state from `last_event` alone.
            return replace(data, last_event=event)

        # Other event families are interesting but don't move any
        # currently-modeled state — record them for diagnostics so
        # subscribers can still react via the last_event attribute.
        return replace(data, last_event=event)

