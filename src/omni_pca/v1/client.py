"""High-level read-only client for v1-over-UDP Omni-Link panels.

Mirrors the v2 :class:`omni_pca.client.OmniClient` API where the v1 wire
protocol can satisfy the same call. Methods that require v2-only opcodes
(e.g. ``RequestProperties``, ``AcknowledgeAlerts``) are intentionally
absent until Phase 2b/2c add their v1 equivalents (streaming
``UploadNames``, no-op or alternate dispatch).

API parity goals (this module):
    get_system_information()                       — same dataclass as v2
    get_system_status()                            — same dataclass as v2
    get_zone_status(start, end)         -> dict    — uses v1 ZoneStatus
    get_unit_status(start, end)         -> dict    — uses v1 UnitStatus
    get_thermostat_status(start, end)   -> dict    — uses v1 ThermostatStatus
    get_aux_status(start, end)          -> dict    — uses v1 AuxiliaryStatus
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator, Callable
from typing import Self

from ..commands import Command, CommandFailedError, SecurityCommandResponse
from ..models import (
    AuxSensorStatus,
    SecurityMode,
    SystemInformation,
    SystemStatus,
    ThermostatStatus,
    UnitStatus,
    ZoneStatus,
)
from ..opcodes import OmniLinkMessageType
from .connection import OmniConnectionV1
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

_DEFAULT_PORT = 4369


class OmniClientV1:
    """Read-only v1-over-UDP Omni-Link client.

    .. code-block:: python

        async with OmniClientV1("192.168.1.9", controller_key=key) as c:
            info = await c.get_system_information()
            zones = await c.get_zone_status(1, 16)
    """

    def __init__(
        self,
        host: str,
        port: int = _DEFAULT_PORT,
        controller_key: bytes = b"",
        timeout: float = 5.0,
        retry_count: int = 3,
    ) -> None:
        self._conn = OmniConnectionV1(
            host=host,
            port=port,
            controller_key=controller_key,
            timeout=timeout,
            retry_count=retry_count,
        )

    @property
    def connection(self) -> OmniConnectionV1:
        return self._conn

    async def __aenter__(self) -> Self:
        await self._conn.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self._conn.close()

    # ---- panel-wide ----------------------------------------------------

    async def get_system_information(self) -> SystemInformation:
        """Fetch model + firmware + dialer phone number.

        Wire format identical to v2 (verified per
        clsOLMsgSystemInformation.cs vs clsOL2MsgSystemInformation.cs);
        we reuse the existing dataclass parser unchanged.
        """
        reply = await self._conn.request(
            OmniLinkMessageType.RequestSystemInformation
        )
        self._expect(reply.opcode, OmniLinkMessageType.SystemInformation)
        return SystemInformation.parse(reply.payload)

    async def get_system_status(self) -> SystemStatus:
        """Fetch panel time, sunrise/sunset, battery reading, area modes."""
        reply = await self._conn.request(
            OmniLinkMessageType.RequestSystemStatus
        )
        self._expect(reply.opcode, OmniLinkMessageType.SystemStatus)
        return parse_v1_system_status(reply.payload)

    # ---- bulk per-object status ----------------------------------------

    async def get_zone_status(
        self, start: int, end: int
    ) -> dict[int, ZoneStatus]:
        return await self._range_status(
            OmniLinkMessageType.RequestZoneStatus,
            OmniLinkMessageType.ZoneStatus,
            start,
            end,
            parse_v1_zone_status,
        )

    async def get_unit_status(
        self, start: int, end: int
    ) -> dict[int, UnitStatus]:
        return await self._range_status(
            OmniLinkMessageType.RequestUnitStatus,
            OmniLinkMessageType.UnitStatus,
            start,
            end,
            parse_v1_unit_status,
        )

    async def get_thermostat_status(
        self, start: int, end: int
    ) -> dict[int, ThermostatStatus]:
        return await self._range_status(
            OmniLinkMessageType.RequestThermostatStatus,
            OmniLinkMessageType.ThermostatStatus,
            start,
            end,
            parse_v1_thermostat_status,
        )

    async def get_aux_status(
        self, start: int, end: int
    ) -> dict[int, AuxSensorStatus]:
        return await self._range_status(
            OmniLinkMessageType.RequestAuxiliaryStatus,
            OmniLinkMessageType.AuxiliaryStatus,
            start,
            end,
            parse_v1_aux_status,
        )

    # ---- discovery (streaming UploadNames) ------------------------------

    async def iter_names(self) -> AsyncIterator[NameRecord]:
        """Stream every defined name from the panel.

        v1 has no per-type name request — a bare ``UploadNames`` triggers
        the panel to dump *all* defined names of *all* types in a fixed
        order (Zone, Unit, Button, Code, Area, Thermostat, Message, …),
        each as a separate ``NameData`` reply that the client must
        ``Acknowledge`` to advance. This iterator handles the lock-step
        protocol and yields each record as it arrives.

        Reference: clsHAC.cs:4418 (sends bare UploadNames),
        OL1ReadConfigHandleResponse (loops over NameData/EOD).
        """
        async for reply in self._conn.iter_streaming(
            OmniLinkMessageType.UploadNames
        ):
            if reply.opcode != int(OmniLinkMessageType.NameData):
                # Defensive — iter_streaming normally only yields
                # non-EOD/NAK replies, so this is a wire-format fault.
                raise OmniProtocolError(
                    f"unexpected opcode {reply.opcode} during UploadNames stream "
                    f"(expected {int(OmniLinkMessageType.NameData)})"
                )
            yield parse_v1_namedata(reply.payload)

    async def list_all_names(self) -> dict[int, dict[int, str]]:
        """Bucket every defined name by ``NameType``.

        Returns ``{name_type: {object_number: name}}``. Useful when HA
        needs all four (zones+units+areas+thermostats) in one pass —
        cheaper than four separate streams since the panel only supports
        one streaming session at a time anyway.
        """
        out: dict[int, dict[int, str]] = {}
        async for rec in self.iter_names():
            out.setdefault(rec.name_type, {})[rec.number] = rec.name
        return out

    async def list_zone_names(self) -> dict[int, str]:
        return (await self.list_all_names()).get(int(NameType.ZONE), {})

    async def list_unit_names(self) -> dict[int, str]:
        return (await self.list_all_names()).get(int(NameType.UNIT), {})

    async def list_area_names(self) -> dict[int, str]:
        return (await self.list_all_names()).get(int(NameType.AREA), {})

    async def list_thermostat_names(self) -> dict[int, str]:
        return (await self.list_all_names()).get(int(NameType.THERMOSTAT), {})

    async def list_button_names(self) -> dict[int, str]:
        return (await self.list_all_names()).get(int(NameType.BUTTON), {})

    # ---- write methods (Command + ExecuteSecurityCommand) ----------------
    #
    # The Command and ExecuteSecurityCommand payloads are byte-identical
    # between v1 and v2 — only the outer opcode differs (15 vs 20 for
    # Command, 102 vs 74 for ExecuteSecurityCommand). So these methods are
    # near-duplicates of OmniClient's, just routed through the v1 opcodes.
    # Reference: clsOLMsgCommand.cs, clsOLMsgExecuteSecurityCommand.cs.

    async def execute_command(
        self,
        command: Command,
        parameter1: int = 0,
        parameter2: int = 0,
    ) -> None:
        """Send a generic Command (v1 opcode 15).

        Wire payload (4 bytes, identical to v2 form):
            [0] command byte (enuUnitCommand value)
            [1] parameter1   (single byte; brightness, mode, code index, ...)
            [2] parameter2 high byte (BE u16)
            [3] parameter2 low  byte (object number for nearly every command)

        Panel acks with v1 Ack (opcode 5) on success, Nak (6) on failure.
        """
        if not 0 <= parameter1 <= 0xFF:
            raise ValueError(f"parameter1 must fit in a byte: {parameter1}")
        if not 0 <= parameter2 <= 0xFFFF:
            raise ValueError(f"parameter2 must fit in u16: {parameter2}")
        payload = struct.pack(
            ">BBH", int(command), parameter1 & 0xFF, parameter2 & 0xFFFF
        )
        reply = await self._conn.request(OmniLinkMessageType.Command, payload)
        if reply.opcode == int(OmniLinkMessageType.Nak):
            raise CommandFailedError(
                f"panel NAK'd Command {command.name} "
                f"(p1={parameter1}, p2={parameter2})"
            )
        if reply.opcode != int(OmniLinkMessageType.Ack):
            raise CommandFailedError(
                f"unexpected reply to Command {command.name}: opcode={reply.opcode}"
            )

    async def execute_security_command(
        self,
        area: int,
        mode: SecurityMode,
        code: int,
    ) -> None:
        """Arm or disarm a security area (v1 opcode 102).

        Wire payload (6 bytes, identical to v2 form — clsOLMsgExecuteSecurityCommand.cs):
            [0] area number (1-based)
            [1] security mode byte (raw enuSecurityMode 0..7)
            [2] code digit 1 (thousands)
            [3] code digit 2 (hundreds)
            [4] code digit 3 (tens)
            [5] code digit 4 (ones)

        Panel responds with:
          * ``ExecuteSecurityCommandResponse`` (103) carrying a status byte
            (0 = success, see :class:`SecurityCommandResponse` for others), or
          * ``Ack`` (5) on success without structured response, or
          * ``Nak`` (6) on flat-out refusal.

        Raises:
            ValueError: ``area`` not 1..255 or ``code`` not 0..9999.
            CommandFailedError: panel Nak'd OR response status was non-zero;
                ``failure_code`` carries the raw status byte when present.
        """
        if not 1 <= area <= 0xFF:
            raise ValueError(f"area out of range: {area}")
        if not 0 <= code <= 9999:
            raise ValueError(f"code out of range (0000-9999): {code}")
        d1 = (code // 1000) % 10
        d2 = (code // 100) % 10
        d3 = (code // 10) % 10
        d4 = code % 10
        payload = bytes([area & 0xFF, int(mode) & 0xFF, d1, d2, d3, d4])
        reply = await self._conn.request(
            OmniLinkMessageType.ExecuteSecurityCommand, payload
        )
        if reply.opcode == int(OmniLinkMessageType.Nak):
            raise CommandFailedError(
                f"panel NAK'd ExecuteSecurityCommand "
                f"(area={area}, mode={mode.name})"
            )
        if reply.opcode == int(OmniLinkMessageType.ExecuteSecurityCommandResponse):
            if not reply.payload:
                raise CommandFailedError(
                    "ExecuteSecurityCommandResponse with empty payload"
                )
            status = reply.payload[0]
            if status != int(SecurityCommandResponse.SUCCESS):
                try:
                    label = SecurityCommandResponse(status).name
                except ValueError:
                    label = f"unknown({status})"
                raise CommandFailedError(
                    f"ExecuteSecurityCommand failed: {label}",
                    failure_code=status,
                )
            return
        if reply.opcode == int(OmniLinkMessageType.Ack):
            return
        raise CommandFailedError(
            f"unexpected reply to ExecuteSecurityCommand: opcode={reply.opcode}"
        )

    async def acknowledge_alerts(self) -> None:
        """V1 has no AcknowledgeAlerts opcode — silently no-op.

        v2 introduced :attr:`OmniLink2MessageType.AcknowledgeAlerts` (60)
        as a dedicated panel-wide ack; v1 panels expect alerts to be
        cleared by per-area arming or by user action at the keypad. To
        keep the v1↔v2 method shape parallel, this method is a no-op so
        HA service callers don't need a per-transport branch.
        """
        return

    # ---- thin command wrappers (one-liner conveniences) ------------------

    async def turn_unit_on(self, index: int) -> None:
        await self.execute_command(Command.UNIT_ON, parameter2=index)

    async def turn_unit_off(self, index: int) -> None:
        await self.execute_command(Command.UNIT_OFF, parameter2=index)

    async def set_unit_level(self, index: int, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"percent must be 0..100: {percent}")
        await self.execute_command(
            Command.UNIT_LEVEL, parameter1=percent, parameter2=index
        )

    async def bypass_zone(self, index: int, code: int = 0) -> None:
        await self.execute_command(
            Command.BYPASS_ZONE, parameter1=code, parameter2=index
        )

    async def restore_zone(self, index: int, code: int = 0) -> None:
        await self.execute_command(
            Command.RESTORE_ZONE, parameter1=code, parameter2=index
        )

    async def execute_button(self, index: int) -> None:
        await self.execute_command(Command.EXECUTE_BUTTON, parameter2=index)

    async def set_thermostat_system_mode(self, index: int, mode_value: int) -> None:
        if not 0 <= mode_value <= 0xFF:
            raise ValueError(f"mode value must fit in a byte: {mode_value}")
        await self.execute_command(
            Command.SET_THERMOSTAT_SYSTEM_MODE,
            parameter1=mode_value,
            parameter2=index,
        )

    async def set_thermostat_fan_mode(self, index: int, mode_value: int) -> None:
        await self.execute_command(
            Command.SET_THERMOSTAT_FAN_MODE,
            parameter1=mode_value,
            parameter2=index,
        )

    async def set_thermostat_hold_mode(self, index: int, mode_value: int) -> None:
        await self.execute_command(
            Command.SET_THERMOSTAT_HOLD_MODE,
            parameter1=mode_value,
            parameter2=index,
        )

    async def set_thermostat_heat_setpoint_raw(
        self, index: int, raw_temp: int
    ) -> None:
        """Set the heat setpoint by raw byte value (Omni temperature scale).

        Use the same :func:`omni_temp_to_celsius` family of helpers from
        :mod:`omni_pca.models` to convert from °C/°F if needed.
        """
        if not 0 <= raw_temp <= 0xFF:
            raise ValueError(f"raw_temp must fit in a byte: {raw_temp}")
        await self.execute_command(
            Command.SET_THERMOSTAT_HEAT_SETPOINT,
            parameter1=raw_temp,
            parameter2=index,
        )

    async def set_thermostat_cool_setpoint_raw(
        self, index: int, raw_temp: int
    ) -> None:
        if not 0 <= raw_temp <= 0xFF:
            raise ValueError(f"raw_temp must fit in a byte: {raw_temp}")
        await self.execute_command(
            Command.SET_THERMOSTAT_COOL_SETPOINT,
            parameter1=raw_temp,
            parameter2=index,
        )

    # ---- helpers --------------------------------------------------------

    async def _range_status[T](
        self,
        request_op: OmniLinkMessageType,
        reply_op: OmniLinkMessageType,
        start: int,
        end: int,
        parser: Callable[[bytes, int], list[T]],
    ) -> dict[int, T]:
        if not 1 <= start <= end <= 0xFFFF:
            raise ValueError(
                f"invalid range: start={start}, end={end} "
                f"(must be 1..65535 with start<=end)"
            )
        # v1 has two payload forms (clsOLMsgRequestUnitStatus.cs:18-31):
        # short (3-byte msg with 1-byte start+end) when both ≤ 255, long
        # (5-byte msg with BE u16 start+end) otherwise. The panel picks
        # the right reply format based on what it received.
        if start <= 0xFF and end <= 0xFF:
            payload = bytes([start, end])
        else:
            payload = bytes(
                [(start >> 8) & 0xFF, start & 0xFF,
                 (end >> 8) & 0xFF,   end & 0xFF]
            )
        reply = await self._conn.request(request_op, payload)
        self._expect(reply.opcode, reply_op)
        records = parser(reply.payload, start)
        return {r.index: r for r in records}  # type: ignore[attr-defined]

    @staticmethod
    def _expect(actual: int, expected: OmniLinkMessageType) -> None:
        if actual == int(OmniLinkMessageType.Nak):
            raise OmniNakError(
                f"panel NAK'd request expecting opcode {int(expected)} "
                f"({expected.name})"
            )
        if actual != int(expected):
            raise OmniProtocolError(
                f"unexpected reply opcode {actual}, want {int(expected)} "
                f"({expected.name})"
            )


class OmniNakError(RuntimeError):
    """Panel returned the v1 Nak opcode (6) instead of the expected reply.

    Thrown when a feature the panel doesn't support is requested — e.g.
    ``RequestZoneExtendedStatus`` on firmware 2.12 NAKs because only the
    non-extended ``RequestZoneStatus`` is supported.
    """


class OmniProtocolError(RuntimeError):
    """Panel returned a reply opcode neither matching nor a NAK."""
