"""V2-shape adapter over :class:`OmniClientV1`.

The Home Assistant coordinator was written against :class:`omni_pca.client.OmniClient`
(the v2 API). When the user configures ``transport=udp`` we need a client
that *looks* like ``OmniClient`` but speaks v1-over-UDP underneath.

This adapter exposes only the methods the coordinator and entity
platforms actually call. Where v1 lacks a v2 opcode (Properties for
zones/units/areas, AcknowledgeAlerts), we synthesize a sensible
fallback rather than raise — HA users shouldn't have to care that their
panel is on a different wire protocol.

What the adapter does:

* **Discovery (``list_*_names``)**: delegates to ``OmniClientV1`` (which
  drives the streaming ``UploadNames`` flow once per call).
* **Properties (``get_object_properties``)**: synthesizes a minimal
  ``*Properties`` dataclass from the name alone. v1 has no Properties
  opcode, so we can't fetch zone_type / unit_type / area_alarms / etc.
  Defaults are zero — entity platforms read mostly the name + the live
  ``*Status`` snapshot, so this works for the common case.
* **Bulk status (``get_extended_status``)**: routes Zone/Unit/Thermostat/
  AuxSensor through the v1 typed ``get_*_status`` calls and returns the
  resulting dataclass list (same shape v2 produces).
* **Area status (``get_object_status(AREA, …)``)**: derives ``AreaStatus``
  records from the per-area mode bytes in v1 ``SystemStatus`` — v1 has
  no per-area status opcode and the modes are the only thing the panel
  reports on UDP.
* **Events (``events()``)**: returns an :class:`EventStream` filtered on
  v1's SystemEvents opcode (35) instead of v2's (55). Word format is
  identical, so the existing typed-event decoder works unchanged.
* **Writes**: pass-through to the underlying ``OmniClientV1`` methods,
  whose Command / ExecuteSecurityCommand payloads are byte-identical
  to v2 — only the opcode differs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Self

from ..commands import Command
from ..events import EventStream, SystemEvent
from ..models import (
    AreaProperties,
    AreaStatus,
    AuxSensorStatus,
    ButtonProperties,
    ObjectType,
    SecurityMode,
    SystemInformation,
    SystemStatus,
    ThermostatProperties,
    ThermostatStatus,
    UnitProperties,
    UnitStatus,
    ZoneProperties,
    ZoneStatus,
)
from ..opcodes import OmniLinkMessageType
from .client import OmniClientV1
from .connection import OmniConnectionV1

# Type used by coordinator for object_type arg (the IntEnum in
# omni_pca.client is just a re-export of models.ObjectType).
_ObjectType = ObjectType

_DEFAULT_PORT = 4369


class OmniClientV1Adapter:
    """V2-shaped facade over :class:`OmniClientV1`.

    Construct with the same kwargs as :class:`OmniClient`; the
    coordinator does not need to know which one it has.
    """

    def __init__(
        self,
        host: str,
        port: int = _DEFAULT_PORT,
        controller_key: bytes = b"",
        timeout: float = 5.0,
        retry_count: int = 3,
        **_ignored,
    ) -> None:
        # `transport=` and similar kwargs are accepted-and-ignored so the
        # coordinator's construction call stays identical across v1/v2.
        self._client = OmniClientV1(
            host=host,
            port=port,
            controller_key=controller_key,
            timeout=timeout,
            retry_count=retry_count,
        )

    # ---- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self._client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._client.__aexit__(exc_type, exc, tb)

    @property
    def connection(self) -> OmniConnectionV1:
        """Underlying :class:`OmniConnectionV1` — used by the coordinator's
        low-level walks. v1's connection has the same ``unsolicited()`` /
        ``request()`` surface as v2's, just a different wire dialect.
        """
        return self._client.connection

    # ---- panel-wide reads ----------------------------------------------

    async def get_system_information(self) -> SystemInformation:
        return await self._client.get_system_information()

    async def get_system_status(self) -> SystemStatus:
        return await self._client.get_system_status()

    # ---- discovery (cached once per coordinator setup) -----------------
    #
    # The coordinator calls list_*_names() once per object type. Each
    # call drives a fresh UploadNames stream, which on this panel takes
    # ~250ms per ~100 names. We cache the full bucketed dict on first
    # call so the four list_*_names() calls + several synthesize-
    # properties calls all share one network roundtrip.

    async def _ensure_names(self) -> dict[int, dict[int, str]]:
        cached = getattr(self, "_name_cache", None)
        if cached is None:
            cached = await self._client.list_all_names()
            self._name_cache = cached
        return cached

    def _invalidate_names(self) -> None:
        """Force the next discovery call to re-stream UploadNames."""
        self._name_cache = None  # type: ignore[assignment]

    async def list_zone_names(self) -> dict[int, str]:
        return (await self._ensure_names()).get(1, {})  # NameType.ZONE

    async def list_unit_names(self) -> dict[int, str]:
        return (await self._ensure_names()).get(2, {})  # NameType.UNIT

    async def list_area_names(self) -> dict[int, str]:
        """Return area names, falling back to "Area N" when stream is empty.

        Most v1 panels don't expose user-assigned area names — the slots
        exist (8 for Omni Pro II) but the .pca file leaves them zero-
        filled. HA needs *something* to label each area entity, so we
        synthesize "Area 1".."Area 8" as a fixed-size fallback. The 8
        is the Omni Pro II cap; we cap here even when ``SystemStatus``
        reports more mode bytes because the long-form SystemStatus
        payload mixes in EE-expansion telemetry past byte 22.
        """
        named = (await self._ensure_names()).get(5, {})  # NameType.AREA
        if named:
            return named
        return {i: f"Area {i}" for i in range(1, 9)}

    async def list_thermostat_names(self) -> dict[int, str]:
        return (await self._ensure_names()).get(6, {})  # NameType.THERMOSTAT

    async def list_button_names(self) -> dict[int, str]:
        return (await self._ensure_names()).get(3, {})  # NameType.BUTTON

    async def list_code_names(self) -> dict[int, str]:
        return (await self._ensure_names()).get(4, {})  # NameType.CODE

    async def list_message_names(self) -> dict[int, str]:
        return (await self._ensure_names()).get(7, {})  # NameType.MESSAGE

    # ---- programs ------------------------------------------------------

    def iter_programs(self):
        """Forward to OmniClientV1.iter_programs (streaming UploadPrograms).

        Same async-iterator shape as :meth:`OmniClient.iter_programs` so the
        coordinator does not need a transport branch.
        """
        return self._client.iter_programs()

    async def download_program(self, slot: int, program) -> None:
        """v1 forwarder — raises NotImplementedError. See client.py."""
        await self._client.download_program(slot, program)

    async def clear_program(self, slot: int) -> None:
        await self._client.clear_program(slot)

    # ---- properties synthesis ------------------------------------------

    async def get_object_properties(
        self, object_type: ObjectType, index: int
    ) -> ZoneProperties | UnitProperties | AreaProperties | ThermostatProperties | None:
        """Synthesize a Properties dataclass from the name alone.

        v1 has no ``RequestProperties`` opcode; the rich fields v2 carries
        (zone_type, unit areas bitfield, exit/entry delays, …) simply
        aren't reachable on UDP. We return a minimal dataclass with just
        ``index`` + ``name`` populated and everything else defaulted to
        0/False so entity setup doesn't need a transport branch.

        Returns ``None`` if the object isn't defined (no name and not in
        the default area-fallback range), which mirrors v2's behavior
        when ``RequestProperties`` walks past the last defined object.
        """
        names = await self._ensure_names()
        if object_type == ObjectType.ZONE:
            name = names.get(1, {}).get(index)
            if not name:
                return None
            return ZoneProperties(
                index=index, name=name, zone_type=0, area=1,
                options=0, status=0, loop=0,
            )
        if object_type == ObjectType.UNIT:
            name = names.get(2, {}).get(index)
            if not name:
                return None
            return UnitProperties(
                index=index, name=name, unit_type=0,
                status=0, time=0, areas=0,
            )
        if object_type == ObjectType.AREA:
            # Use the same fallback logic as list_area_names so HA always
            # gets at least the 8 default-area entries.
            label = (await self.list_area_names()).get(index)
            if label is None:
                return None
            return AreaProperties(
                index=index, name=label, mode=0, alarms=0,
                enabled=True, entry_delay=0, exit_delay=0,
            )
        if object_type == ObjectType.THERMOSTAT:
            name = names.get(6, {}).get(index)
            if not name:
                return None
            return ThermostatProperties(
                index=index, name=name, thermostat_type=0,
                communicating=True,
            )
        if object_type == ObjectType.BUTTON:
            name = names.get(3, {}).get(index)
            if not name:
                return None
            return ButtonProperties(index=index, name=name)
        return None

    # ---- bulk status ---------------------------------------------------

    # Per-type max records per chunk. Empirically firmware 2.12 caps unit
    # responses around 62 records regardless of the MessageLength byte
    # limit; other types follow similar conservative caps. We chunk well
    # under those thresholds to leave headroom for any per-firmware
    # variance and the AES zero-padding the wire frames add.
    _CHUNK_SIZES: dict[int, int] = {
        ObjectType.ZONE: 80,         # 2 B/rec, panel caps high enough
        ObjectType.UNIT: 40,         # firmware 2.12 NAKs at 63+ records
        ObjectType.THERMOSTAT: 30,
        ObjectType.AUXILIARY: 60,
    }

    async def get_extended_status(
        self,
        object_type: ObjectType,
        start: int,
        end: int | None = None,
    ) -> list:
        """Route v2 ``get_extended_status`` to the matching v1 typed call.

        v1 panels (Omni Pro II) can have 511 units across a sparse
        address space. We chunk wide ranges into per-type-sized batches
        and concatenate the records — same effect for the caller, only
        the wire transcript is different.
        """
        last = end if end is not None else start
        if object_type == ObjectType.ZONE:
            fetch = self._client.get_zone_status
        elif object_type == ObjectType.UNIT:
            fetch = self._client.get_unit_status
        elif object_type == ObjectType.THERMOSTAT:
            fetch = self._client.get_thermostat_status
        elif object_type == ObjectType.AUXILIARY:
            fetch = self._client.get_aux_status
        else:
            raise ValueError(
                f"v1 has no bulk extended-status opcode for {object_type.name}"
            )

        chunk = self._CHUNK_SIZES.get(int(object_type), 40)
        out: dict[int, object] = {}
        cur = start
        while cur <= last:
            chunk_end = min(cur + chunk - 1, last)
            records = await fetch(cur, chunk_end)
            out.update(records)
            cur = chunk_end + 1
        return [out[i] for i in sorted(out)]

    async def get_object_status(
        self,
        object_type: ObjectType,
        start: int,
        end: int | None = None,
    ) -> list:
        """Synthesize AreaStatus from SystemStatus's per-area mode bytes.

        v1 has no per-area status opcode — but the SystemStatus payload
        carries one ``Mode`` byte per area (single-area panels see one
        byte at offset 15, multi-area panels see N consecutive bytes).
        We promote each into an :class:`AreaStatus` with just ``index``
        and ``mode`` populated; entry/exit timers and alarms are zero
        because the protocol doesn't expose them at this level.

        For non-area object types we fall back to extended-status, which
        on v1 maps to the basic typed-status opcodes (which is what the
        v2 coordinator actually wants anyway since v2's basic and
        extended status are interchangeable in shape).
        """
        if object_type != ObjectType.AREA:
            return await self.get_extended_status(object_type, start, end)

        last = end if end is not None else start
        status = await self._client.get_system_status()
        # First N bytes of area_alarms are valid area modes; the rest are
        # EE-expansion data on long SystemStatus payloads (firmware 2.12
        # length=39 form). We can't reliably tell where modes stop, so
        # match against the list_area_names() count from the same
        # SystemStatus.
        area_count = max(1, min(8, len(status.area_alarms)))
        out: list[AreaStatus] = []
        for idx in range(start, min(last, area_count) + 1):
            mode_pair = (
                status.area_alarms[idx - 1] if idx - 1 < len(status.area_alarms)
                else (0, 0)
            )
            out.append(
                AreaStatus(
                    index=idx,
                    mode=mode_pair[0],
                    last_user=0,
                    entry_timer_secs=0,
                    exit_timer_secs=0,
                    alarms=mode_pair[1],
                )
            )
        return out

    # ---- events --------------------------------------------------------

    def events(self) -> AsyncIterator[SystemEvent]:
        """v1-aware EventStream — filters on v1 SystemEvents opcode (35)."""
        return EventStream(
            self._client.connection,
            expected_opcode=int(OmniLinkMessageType.SystemEvents),
        ).__aiter__()

    async def subscribe(
        self, callback: Callable[[object], Awaitable[None]]
    ) -> None:
        """Not used by the coordinator (which prefers ``events()``); kept
        for API parity with :class:`OmniClient`. Raises ``NotImplementedError``
        to flag accidental use — when we need it, copy the v2 implementation.
        """
        raise NotImplementedError(
            "OmniClientV1Adapter.subscribe is not implemented; "
            "use events() instead"
        )

    # ---- writes (pure pass-through) ------------------------------------

    async def execute_command(
        self, command: Command, parameter1: int = 0, parameter2: int = 0
    ) -> None:
        await self._client.execute_command(command, parameter1, parameter2)

    async def execute_security_command(
        self, area: int, mode: SecurityMode, code: int
    ) -> None:
        await self._client.execute_security_command(area, mode, code)

    async def acknowledge_alerts(self) -> None:
        await self._client.acknowledge_alerts()

    async def turn_unit_on(self, index: int) -> None:
        await self._client.turn_unit_on(index)

    async def turn_unit_off(self, index: int) -> None:
        await self._client.turn_unit_off(index)

    async def set_unit_level(self, index: int, percent: int) -> None:
        await self._client.set_unit_level(index, percent)

    async def bypass_zone(self, index: int, code: int = 0) -> None:
        await self._client.bypass_zone(index, code)

    async def restore_zone(self, index: int, code: int = 0) -> None:
        await self._client.restore_zone(index, code)

    async def execute_button(self, index: int) -> None:
        await self._client.execute_button(index)

    async def execute_program(self, index: int) -> None:
        """Run a panel program by index.

        v1 ``enuUnitCommand.Execute`` (raw byte not aliased in our enum)
        and v2 both use a generic Command. The Command enum's
        ``EXECUTE_PROGRAM`` value works on both because the on-the-wire
        Command body is byte-identical.
        """
        await self.execute_command(Command.EXECUTE_PROGRAM, parameter2=index)

    async def show_message(self, index: int, beep: bool = True) -> None:
        await self.execute_command(
            Command.SHOW_MESSAGE_WITH_BEEP if beep else Command.SHOW_MESSAGE_NO_BEEP,
            parameter2=index,
        )

    async def clear_message(self, index: int) -> None:
        await self.execute_command(Command.CLEAR_MESSAGE, parameter2=index)

    async def set_thermostat_system_mode(self, index: int, mode_value: int) -> None:
        await self._client.set_thermostat_system_mode(index, mode_value)

    async def set_thermostat_fan_mode(self, index: int, mode_value: int) -> None:
        await self._client.set_thermostat_fan_mode(index, mode_value)

    async def set_thermostat_hold_mode(self, index: int, mode_value: int) -> None:
        await self._client.set_thermostat_hold_mode(index, mode_value)

    async def set_thermostat_heat_setpoint_raw(
        self, index: int, raw_temp: int
    ) -> None:
        await self._client.set_thermostat_heat_setpoint_raw(index, raw_temp)

    async def set_thermostat_cool_setpoint_raw(
        self, index: int, raw_temp: int
    ) -> None:
        await self._client.set_thermostat_cool_setpoint_raw(index, raw_temp)
