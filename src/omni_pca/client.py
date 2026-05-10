"""High-level async client for the HAI/Leviton Omni-Link II protocol.

This wraps :class:`OmniConnection` with typed methods that send the
appropriate v2 request opcode and parse the reply payload into one of
the dataclasses in :mod:`omni_pca.models`.

Conventions:
    * Indices are 1-based on the wire (zone 1 is index=1, not 0).
    * ``RequestProperties`` uses ``relative_direction = 0`` for an exact
      lookup (panel returns just that index, or NAK/EOD if absent).
    * Walking with ``relative_direction = 1`` returns each next defined
      object, used by the ``list_*`` helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
from collections.abc import Awaitable, Callable, Sequence
from enum import IntEnum
from types import TracebackType
from typing import Self

from .commands import Command, CommandFailedError, SecurityCommandResponse
from .connection import (
    ConnectionError as OmniConnectionError,
)
from .connection import (
    OmniConnection,
    RequestTimeoutError,
)
from .message import Message
from .models import (
    OBJECT_TYPE_TO_STATUS,
    AreaProperties,
    AreaStatus,
    FanMode,
    HoldMode,
    HvacMode,
    PropertiesReply,
    SecurityMode,
    StatusReply,
    SystemInformation,
    SystemStatus,
    UnitProperties,
    ZoneProperties,
)
from .models import (
    ObjectType as ModelObjectType,
)
from .opcodes import OmniLink2MessageType


class ObjectType(IntEnum):
    """``RequestProperties`` object-type discriminator (matches enuObjectType)."""

    ZONE = 1
    UNIT = 2
    BUTTON = 3
    CODE = 4
    AREA = 5
    THERMOSTAT = 6
    MESSAGE = 7
    AUX_SENSOR = 8
    AUDIO_SOURCE = 9
    AUDIO_ZONE = 10
    EXP_ENCLOSURE = 11
    CONSOLE = 12
    USER_SETTING = 13
    ACCESS_CONTROL = 14


# Maps the request side to the parser side. Only types we actively
# support get an entry; the rest fall through to a generic raw-payload
# return for now.
_PROPERTIES_PARSERS: dict[ObjectType, type[PropertiesReply]] = {
    ObjectType.ZONE: ZoneProperties,
    ObjectType.UNIT: UnitProperties,
    ObjectType.AREA: AreaProperties,
}


# Per-object-type record sizes for a basic Status (opcode 35) reply, where
# (unlike ExtendedStatus) there is no per-record length byte and the size
# is hard-coded in the wire format. Source: clsOL2MsgStatus.cs:13-27.
_STATUS_RECORD_SIZES: dict[int, int] = {
    1: 4,   # enuObjectType.Zone        — number(2) + status + loop
    2: 5,   # enuObjectType.Unit        — number(2) + state + time(2)
    5: 6,   # enuObjectType.Area        — number(2) + mode + alarms + entry + exit
    6: 9,   # enuObjectType.Thermostat  — number(2) + status + 6 bytes (status..hold)
    7: 3,   # enuObjectType.Message     — number(2) + status
    8: 6,   # enuObjectType.Auxillary   — number(2) + output + temp + low + high
    10: 6,  # enuObjectType.AudioZone   — number(2) + power + source + volume + mute
    11: 4,  # enuObjectType.Expansion   — number(2) + status + battery
    13: 5,  # enuObjectType.UserSetting — number(2) + type + value(2)
    15: 5,  # enuObjectType.AccessControlLock — number(2) + status + duration(2)
}


class OmniClient:
    """High-level async Omni-Link II client.

    Use as an async context manager, then call typed methods:

    .. code-block:: python

        async with OmniClient(host, port=4369, controller_key=KEY) as client:
            info = await client.get_system_information()
            zones = await client.list_zone_names()
    """

    def __init__(
        self,
        host: str,
        port: int = 4369,
        *,
        controller_key: bytes,
        timeout: float = 5.0,
    ) -> None:
        self._conn = OmniConnection(
            host=host,
            port=port,
            controller_key=controller_key,
            timeout=timeout,
        )
        self._subscriber_task: asyncio.Task[None] | None = None

    # ---- lifecycle -------------------------------------------------------

    async def __aenter__(self) -> Self:
        await self._conn.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._subscriber_task is not None and not self._subscriber_task.done():
            self._subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._subscriber_task
        await self._conn.close()

    @property
    def connection(self) -> OmniConnection:
        """The underlying low-level connection (for advanced use)."""
        return self._conn

    # ---- typed requests --------------------------------------------------

    async def get_system_information(self) -> SystemInformation:
        reply = await self._conn.request(OmniLink2MessageType.RequestSystemInformation)
        self._expect(reply, OmniLink2MessageType.SystemInformation)
        return SystemInformation.parse(reply.payload)

    async def get_system_status(self) -> SystemStatus:
        reply = await self._conn.request(OmniLink2MessageType.RequestSystemStatus)
        self._expect(reply, OmniLink2MessageType.SystemStatus)
        return SystemStatus.parse(reply.payload)

    async def get_object_properties(
        self,
        object_type: ObjectType,
        index: int,
    ) -> PropertiesReply:
        """Fetch one Properties reply for the given object.

        Returns the appropriate dataclass for ``object_type``. Raises
        :class:`ValueError` if the panel doesn't have an object at that
        index, or :class:`NotImplementedError` if we don't yet have a
        parser for that object type.
        """
        parser = _PROPERTIES_PARSERS.get(object_type)
        if parser is None:
            raise NotImplementedError(
                f"no parser for object type {object_type.name}"
            )
        payload = self._build_request_properties_payload(
            object_type=object_type,
            index=index,
            relative_direction=0,
        )
        reply = await self._conn.request(
            OmniLink2MessageType.RequestProperties, payload
        )
        if reply.opcode == OmniLink2MessageType.EOD:
            raise ValueError(
                f"no {object_type.name} at index {index} (panel returned EOD)"
            )
        if reply.opcode == OmniLink2MessageType.Nak:
            raise ValueError(
                f"panel NAK'd Properties request for {object_type.name}#{index}"
            )
        self._expect(reply, OmniLink2MessageType.Properties)
        return parser.parse(reply.payload)

    # ---- commands --------------------------------------------------------

    async def execute_command(
        self,
        command: Command,
        parameter1: int = 0,
        parameter2: int = 0,
    ) -> None:
        """Send a generic Command (opcode 20).

        Most state-change operations on lights, scenes, zones, thermostats,
        scenes, audio zones, etc. flow through here. The panel acks with
        an :attr:`OmniLink2MessageType.Ack`; specific resulting state must
        be re-polled (or you can subscribe to the unsolicited push stream
        to see the corresponding ExtendedStatus push the panel emits).

        Wire opcode: 20 (Command).
        Wire payload (4 bytes, from clsOL2MsgCommand.cs:5-57):
            [0] command byte (this enum value)
            [1] parameter1   (single byte; brightness, mode, code index, ...)
            [2] parameter2 high byte (BE u16)
            [3] parameter2 low  byte  (object number for nearly every command)

        Reference: clsOL2MsgCommand.cs.
        """
        if not 0 <= parameter1 <= 0xFF:
            raise ValueError(f"parameter1 must fit in a byte: {parameter1}")
        if not 0 <= parameter2 <= 0xFFFF:
            raise ValueError(f"parameter2 must fit in u16: {parameter2}")
        payload = struct.pack(
            ">BBH", int(command), parameter1 & 0xFF, parameter2 & 0xFFFF
        )
        reply = await self._conn.request(OmniLink2MessageType.Command, payload)
        if reply.opcode == OmniLink2MessageType.Nak:
            raise CommandFailedError(
                f"panel NAK'd Command {command.name} "
                f"(p1={parameter1}, p2={parameter2})"
            )
        if reply.opcode != OmniLink2MessageType.Ack:
            raise CommandFailedError(
                f"unexpected reply to Command {command.name}: opcode={reply.opcode}"
            )

    async def execute_security_command(
        self,
        area: int,
        mode: SecurityMode,
        code: int,
    ) -> AreaStatus | None:
        """Arm or disarm a security area.

        The panel validates the code against its enabled-codes list for
        that area; on failure it returns an
        :attr:`OmniLink2MessageType.ExecuteSecurityCommandResponse` whose
        ``payload[0]`` is one of the :class:`SecurityCommandResponse`
        values. On success the panel may either return an Ack or push an
        ExtendedStatus update for the affected area; we surface only the
        response code (success/failure) and return ``None`` for the
        success path because the synchronous reply does not carry a full
        :class:`AreaStatus` record. Re-poll via :meth:`get_object_status`
        if you need the post-command state.

        Wire opcode: 74 (ExecuteSecurityCommand).
        Wire payload (6 bytes, from clsOL2MsgExecuteSecurityCommand.cs:5-90):
            [0] area number (1-based)
            [1] security mode byte (raw enuSecurityMode 0..7)
            [2] code digit 1 (thousands)
            [3] code digit 2 (hundreds)
            [4] code digit 3 (tens)
            [5] code digit 4 (ones)

        Raises:
            ValueError: ``area`` not 1..255 or ``code`` not 0..9999.
            CommandFailedError: panel Nak'd the request, or the
                ExecuteSecurityCommandResponse status byte is non-zero.
                The structured failure code is exposed on
                ``CommandFailedError.failure_code``.

        Reference: clsOL2MsgExecuteSecurityCommand.cs,
        clsOL2MsgExecuteSecurityCommandResponse.cs.
        """
        if not 1 <= area <= 0xFF:
            raise ValueError(f"area out of range: {area}")
        if not 0 <= code <= 9999:
            raise ValueError(f"code out of range (0000-9999): {code}")
        # Match the C# digit-packing exactly (clsOL2MsgExecuteSecurityCommand.cs:36-41).
        d1 = (code // 1000) % 10
        d2 = (code // 100) % 10
        d3 = (code // 10) % 10
        d4 = code % 10
        payload = bytes([area & 0xFF, int(mode) & 0xFF, d1, d2, d3, d4])
        reply = await self._conn.request(
            OmniLink2MessageType.ExecuteSecurityCommand, payload
        )
        if reply.opcode == OmniLink2MessageType.Nak:
            raise CommandFailedError(
                f"panel NAK'd ExecuteSecurityCommand "
                f"(area={area}, mode={mode.name})"
            )
        if reply.opcode == OmniLink2MessageType.ExecuteSecurityCommandResponse:
            if not reply.payload:
                raise CommandFailedError(
                    "ExecuteSecurityCommandResponse with empty payload"
                )
            status = reply.payload[0]
            if status != SecurityCommandResponse.SUCCESS:
                try:
                    label = SecurityCommandResponse(status).name
                except ValueError:
                    label = f"unknown({status})"
                raise CommandFailedError(
                    f"ExecuteSecurityCommand failed: {label}",
                    failure_code=status,
                )
            return None
        if reply.opcode == OmniLink2MessageType.Ack:
            return None
        raise CommandFailedError(
            f"unexpected reply to ExecuteSecurityCommand: opcode={reply.opcode}"
        )

    async def acknowledge_alerts(self) -> None:
        """Acknowledge all outstanding alerts/troubles on the panel.

        Wire opcode: 60 (AcknowledgeAlerts). No payload, panel acks.

        Reference: enuOmniLink2MessageType.AcknowledgeAlerts.
        """
        reply = await self._conn.request(OmniLink2MessageType.AcknowledgeAlerts)
        if reply.opcode == OmniLink2MessageType.Nak:
            raise CommandFailedError("panel NAK'd AcknowledgeAlerts")
        if reply.opcode != OmniLink2MessageType.Ack:
            raise CommandFailedError(
                f"unexpected reply to AcknowledgeAlerts: opcode={reply.opcode}"
            )

    async def get_object_status(
        self,
        object_type: ModelObjectType,
        start: int,
        end: int | None = None,
    ) -> Sequence[StatusReply]:
        """Request basic Status (opcode 34/35) for a range of objects.

        ``end=None`` requests just the single object at ``start``. Returns
        a list of the appropriate ``*Status`` dataclass instances, parsed
        from each fixed-size record in the reply.

        Unlike :meth:`get_extended_status`, the basic Status reply has NO
        per-record ``object_length`` byte — record sizes are hard-coded
        per object type (see ``clsOL2MsgStatus.cs:13-27``).

        Wire opcode: 34 (RequestStatus) -> 35 (Status).
        RequestStatus payload (5 bytes, clsOL2MsgRequestStatus.cs:5-41):
            [0]    object type (enuObjectType)
            [1..2] starting number (BE u16)
            [3..4] ending number   (BE u16)

        Status reply payload layout (clsOL2MsgStatus.cs):
            [0]    object type
            [1..]  N records of size :data:`_STATUS_RECORD_SIZES[object_type]`

        Reference: clsOL2MsgRequestStatus.cs, clsOL2MsgStatus.cs.
        """
        return await self._fetch_status_range(
            object_type=object_type,
            start=start,
            end=end,
            request_opcode=OmniLink2MessageType.RequestStatus,
            reply_opcode=OmniLink2MessageType.Status,
            header_bytes=1,  # just object_type
            record_sizes=_STATUS_RECORD_SIZES,
        )

    async def get_extended_status(
        self,
        object_type: ModelObjectType,
        start: int,
        end: int | None = None,
    ) -> Sequence[StatusReply]:
        """Request ExtendedStatus (opcode 58/59) for a range of objects.

        For Thermostats, AuxSensors, dimmable Units, and most other types
        this carries more fields (current temperature, setpoints,
        brightness level, etc.) than the basic Status reply.

        Unlike basic Status, the ExtendedStatus reply has an explicit
        ``object_length`` byte at ``payload[1]`` so the record size doesn't
        have to be hard-coded — we use it as-is.

        Wire opcode: 58 (RequestExtendedStatus) -> 59 (ExtendedStatus).
        RequestExtendedStatus payload (5 bytes, clsOL2MsgRequestExtendedStatus.cs:5-41):
            [0]    object type
            [1..2] starting number (BE u16)
            [3..4] ending number   (BE u16)

        ExtendedStatus reply payload layout (clsOL2MsgExtendedStatus.cs):
            [0]    object type
            [1]    object length (per-record byte count)
            [2..]  N records of ``object_length`` bytes

        Reference: clsOL2MsgRequestExtendedStatus.cs, clsOL2MsgExtendedStatus.cs.
        """
        return await self._fetch_status_range(
            object_type=object_type,
            start=start,
            end=end,
            request_opcode=OmniLink2MessageType.RequestExtendedStatus,
            reply_opcode=OmniLink2MessageType.ExtendedStatus,
            header_bytes=2,  # object_type + object_length
            record_sizes=None,  # take from payload[1]
        )

    # ---- thin command wrappers ------------------------------------------

    async def turn_unit_on(self, index: int) -> None:
        """Turn a unit (light, relay, scene) ON.

        Wire opcode: 20 (Command), command byte = ``Command.UNIT_ON`` (1).
        Reference: enuUnitCommand.On (line 6).
        """
        await self.execute_command(Command.UNIT_ON, parameter2=index)

    async def turn_unit_off(self, index: int) -> None:
        """Turn a unit OFF.

        Wire opcode: 20 (Command), command byte = ``Command.UNIT_OFF`` (0).
        Reference: enuUnitCommand.Off (line 5).
        """
        await self.execute_command(Command.UNIT_OFF, parameter2=index)

    async def set_unit_level(self, index: int, percent: int) -> None:
        """Set a dimmable unit's brightness to ``percent`` (0..100).

        Wire opcode: 20 (Command), command byte = ``Command.UNIT_LEVEL`` (9),
        parameter1 = percent.
        Reference: enuUnitCommand.Level (line 15).
        """
        if not 0 <= percent <= 100:
            raise ValueError(f"percent must be 0..100: {percent}")
        await self.execute_command(
            Command.UNIT_LEVEL, parameter1=percent, parameter2=index
        )

    async def bypass_zone(self, index: int, code: int = 0) -> None:
        """Bypass a zone (1-based).

        Wire opcode: 20 (Command), command byte = ``Command.BYPASS_ZONE`` (4),
        parameter1 = user code index (0 = installer/no-code path),
        parameter2 = zone number.

        Reference: enuUnitCommand.Bypass (line 10).
        """
        await self.execute_command(
            Command.BYPASS_ZONE, parameter1=code, parameter2=index
        )

    async def restore_zone(self, index: int, code: int = 0) -> None:
        """Restore a previously-bypassed zone.

        Wire opcode: 20 (Command), command byte = ``Command.RESTORE_ZONE`` (5),
        parameter1 = user code index, parameter2 = zone number.

        Reference: enuUnitCommand.Restore (line 11).
        """
        await self.execute_command(
            Command.RESTORE_ZONE, parameter1=code, parameter2=index
        )

    async def set_thermostat_system_mode(
        self, index: int, mode: HvacMode
    ) -> None:
        """Change the thermostat's system mode (Off/Heat/Cool/Auto/EmHeat).

        Wire opcode: 20 (Command), command byte =
        ``Command.SET_THERMOSTAT_SYSTEM_MODE`` (68),
        parameter1 = mode value, parameter2 = thermostat number.

        Reference: enuUnitCommand.Mode (line 73).
        """
        await self.execute_command(
            Command.SET_THERMOSTAT_SYSTEM_MODE,
            parameter1=int(mode),
            parameter2=index,
        )

    async def set_thermostat_fan_mode(
        self, index: int, mode: FanMode
    ) -> None:
        """Change the thermostat's fan mode (Auto/On/Cycle).

        Wire opcode: 20 (Command), command byte =
        ``Command.SET_THERMOSTAT_FAN_MODE`` (69).
        Reference: enuUnitCommand.Fan (line 74).
        """
        await self.execute_command(
            Command.SET_THERMOSTAT_FAN_MODE,
            parameter1=int(mode),
            parameter2=index,
        )

    async def set_thermostat_hold_mode(
        self, index: int, mode: HoldMode
    ) -> None:
        """Change the thermostat's hold mode (Off/Hold/Vacation).

        Wire opcode: 20 (Command), command byte =
        ``Command.SET_THERMOSTAT_HOLD_MODE`` (70).
        Reference: enuUnitCommand.Hold (line 75).
        """
        await self.execute_command(
            Command.SET_THERMOSTAT_HOLD_MODE,
            parameter1=int(mode),
            parameter2=index,
        )

    async def set_thermostat_heat_setpoint_raw(
        self, index: int, raw: int
    ) -> None:
        """Set the heat setpoint, in Omni's raw temperature byte units.

        Convert from C/F at the call site (see
        :func:`omni_pca.models.omni_temp_to_celsius` /
        :func:`omni_pca.models.omni_temp_to_fahrenheit` for the inverse) -
        this layer is deliberately transport-shaped.

        Wire opcode: 20 (Command), command byte =
        ``Command.SET_THERMOSTAT_HEAT_SETPOINT`` (66).
        Reference: enuUnitCommand.SetLowSetPt (line 71).
        """
        if not 0 <= raw <= 0xFF:
            raise ValueError(f"raw setpoint must be a byte: {raw}")
        await self.execute_command(
            Command.SET_THERMOSTAT_HEAT_SETPOINT,
            parameter1=raw,
            parameter2=index,
        )

    async def set_thermostat_cool_setpoint_raw(
        self, index: int, raw: int
    ) -> None:
        """Set the cool setpoint, in Omni's raw temperature byte units.

        Wire opcode: 20 (Command), command byte =
        ``Command.SET_THERMOSTAT_COOL_SETPOINT`` (67).
        Reference: enuUnitCommand.SetHighSetPt (line 72).
        """
        if not 0 <= raw <= 0xFF:
            raise ValueError(f"raw setpoint must be a byte: {raw}")
        await self.execute_command(
            Command.SET_THERMOSTAT_COOL_SETPOINT,
            parameter1=raw,
            parameter2=index,
        )

    async def execute_button(self, index: int) -> None:
        """Run the program assigned to a button.

        Wire opcode: 20 (Command), command byte = ``Command.EXECUTE_BUTTON`` (7).
        Reference: enuUnitCommand.Button (line 13).
        """
        await self.execute_command(Command.EXECUTE_BUTTON, parameter2=index)

    async def execute_program(self, index: int) -> None:
        """Run a stored program by index (1-based).

        Wire opcode: 20 (Command), command byte = ``Command.EXECUTE_PROGRAM`` (104).
        Note: enuUnitCommand calls this ``UserSetting`` historically — we
        rename for clarity since "execute program" matches the user-facing
        verb in the owner manual.

        Reference: enuUnitCommand.UserSetting (line 98).
        """
        await self.execute_command(Command.EXECUTE_PROGRAM, parameter2=index)

    async def show_message(self, index: int, beep: bool = True) -> None:
        """Display a stored message on the panel's keypad.

        Wire opcode: 20 (Command), command byte = ``Command.SHOW_MESSAGE_WITH_BEEP``
        (80) when ``beep=True`` or ``Command.SHOW_MESSAGE_NO_BEEP`` (86) otherwise.

        Reference: enuUnitCommand.ShowMsgWBeep (line 81),
        enuUnitCommand.ShowMsgNoBeep (line 87).
        """
        cmd = (
            Command.SHOW_MESSAGE_WITH_BEEP
            if beep
            else Command.SHOW_MESSAGE_NO_BEEP
        )
        await self.execute_command(cmd, parameter2=index)

    async def clear_message(self, index: int) -> None:
        """Clear a previously-shown message.

        Wire opcode: 20 (Command), command byte = ``Command.CLEAR_MESSAGE`` (82).
        Reference: enuUnitCommand.ClearMsg (line 83).
        """
        await self.execute_command(Command.CLEAR_MESSAGE, parameter2=index)

    # ---- helpers (status) -----------------------------------------------

    async def _fetch_status_range(
        self,
        *,
        object_type: ModelObjectType,
        start: int,
        end: int | None,
        request_opcode: OmniLink2MessageType,
        reply_opcode: OmniLink2MessageType,
        header_bytes: int,
        record_sizes: dict[int, int] | None,
    ) -> Sequence[StatusReply]:
        if not 0 <= start <= 0xFFFF:
            raise ValueError(f"start out of range: {start}")
        end_n = start if end is None else end
        if not 0 <= end_n <= 0xFFFF:
            raise ValueError(f"end out of range: {end_n}")
        if end_n < start:
            raise ValueError(f"end ({end_n}) must be >= start ({start})")

        parser = OBJECT_TYPE_TO_STATUS.get(int(object_type))
        if parser is None:
            raise NotImplementedError(
                f"no status parser for object type {object_type.name}"
            )

        payload = struct.pack(">BHH", int(object_type), start, end_n)
        reply = await self._conn.request(request_opcode, payload)
        if reply.opcode == OmniLink2MessageType.EOD:
            return []
        if reply.opcode == OmniLink2MessageType.Nak:
            raise CommandFailedError(
                f"panel NAK'd {request_opcode.name} for "
                f"{object_type.name}#{start}..{end_n}"
            )
        self._expect(reply, reply_opcode)
        body = reply.payload
        if len(body) < header_bytes:
            raise OmniConnectionError(
                f"{reply_opcode.name} payload too short: {len(body)}"
            )
        if body[0] != int(object_type):
            raise OmniConnectionError(
                f"{reply_opcode.name} object type mismatch: "
                f"sent {int(object_type)}, got {body[0]}"
            )
        if record_sizes is None:
            # ExtendedStatus carries the per-record size at payload[1].
            record_size = body[1]
            records_start = 2
        else:
            record_size = record_sizes.get(int(object_type), 0)
            if record_size == 0:
                raise NotImplementedError(
                    f"no Status record size for {object_type.name}"
                )
            records_start = 1
        records_buf = body[records_start:]
        if record_size == 0:
            return []
        out: list[StatusReply] = []
        for off in range(0, len(records_buf), record_size):
            chunk = records_buf[off : off + record_size]
            if len(chunk) < record_size:
                # Trailing partial record: ignore (panel may pad).
                break
            out.append(parser.parse(chunk))
        return out

    async def list_zone_names(self) -> dict[int, str]:
        """Walk all zones, returning ``{index: name}`` for those with a name set."""
        return await self._walk_named_objects(
            ObjectType.ZONE,
            lambda r: (r.index, r.name) if isinstance(r, ZoneProperties) else None,
        )

    async def list_unit_names(self) -> dict[int, str]:
        return await self._walk_named_objects(
            ObjectType.UNIT,
            lambda r: (r.index, r.name) if isinstance(r, UnitProperties) else None,
        )

    async def list_area_names(self) -> dict[int, str]:
        return await self._walk_named_objects(
            ObjectType.AREA,
            lambda r: (r.index, r.name) if isinstance(r, AreaProperties) else None,
        )

    async def subscribe(
        self, callback: Callable[[Message], Awaitable[None]]
    ) -> None:
        """Run ``callback`` for every unsolicited message until cancelled.

        Spawns a background task. If you call ``subscribe`` more than
        once the previous subscription is cancelled (we don't fan out).
        """
        if self._subscriber_task is not None and not self._subscriber_task.done():
            self._subscriber_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._subscriber_task

        async def _runner() -> None:
            async for msg in self._conn.unsolicited():
                try:
                    await callback(msg)
                except Exception:
                    # Don't let a bad callback kill the subscription;
                    # just log via the connection's logger.
                    import logging

                    logging.getLogger(__name__).exception(
                        "unsolicited callback raised"
                    )

        self._subscriber_task = asyncio.create_task(
            _runner(), name="omni-client-subscriber"
        )

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _expect(reply: Message, expected: OmniLink2MessageType) -> None:
        if reply.opcode != int(expected):
            raise OmniConnectionError(
                f"expected opcode {expected.name} ({int(expected)}), "
                f"got {reply.opcode}"
            )

    @staticmethod
    def _build_request_properties_payload(
        object_type: ObjectType,
        index: int,
        relative_direction: int,
        filter1: int = 0,
        filter2: int = 0,
        filter3: int = 0,
    ) -> bytes:
        """Build the 7-byte payload for a RequestProperties (opcode 32) message.

        Layout (clsOL2MsgRequestProperties.cs, after stripping opcode):
            0       object type
            1..2    index (BE ushort)
            3       relative direction (signed: 0=exact, +1=next, -1=prev)
            4..6    filters (per-type bitmasks)
        """
        if not 0 <= index <= 0xFFFF:
            raise ValueError(f"index out of range: {index}")
        rd = relative_direction & 0xFF
        return struct.pack(
            ">BHBBBB",
            int(object_type),
            index,
            rd,
            filter1,
            filter2,
            filter3,
        )

    async def _walk_named_objects(
        self,
        object_type: ObjectType,
        extract: Callable[[PropertiesReply], tuple[int, str] | None],
    ) -> dict[int, str]:
        """Walk every defined object of ``object_type`` and collect non-empty names.

        We use ``relative_direction=1`` (next) starting from index 0 to
        let the panel hand us each defined object in turn until it
        returns EOD (end-of-data, opcode 3).
        """
        names: dict[int, str] = {}
        cursor = 0
        # Bound the walk to the protocol max (ushort) just in case the
        # panel keeps echoing.
        for _ in range(0xFFFF):
            payload = self._build_request_properties_payload(
                object_type=object_type,
                index=cursor,
                relative_direction=1,
            )
            try:
                reply = await self._conn.request(
                    OmniLink2MessageType.RequestProperties, payload
                )
            except RequestTimeoutError:
                break
            if reply.opcode == OmniLink2MessageType.EOD:
                break
            if reply.opcode != OmniLink2MessageType.Properties:
                break
            parser = _PROPERTIES_PARSERS.get(object_type)
            if parser is None:  # pragma: no cover - guarded above
                break
            parsed = parser.parse(reply.payload)
            pair = extract(parsed)
            if pair is not None and pair[1]:
                names[pair[0]] = pair[1]
            # Advance: ask for the next index after the one we just got.
            cursor = parsed.index
            if cursor >= 0xFFFF:
                break
        return names
