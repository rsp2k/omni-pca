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
from collections.abc import Awaitable, Callable
from enum import IntEnum
from types import TracebackType
from typing import Self

from .connection import (
    ConnectionError as OmniConnectionError,
)
from .connection import (
    OmniConnection,
    RequestTimeoutError,
)
from .message import Message
from .models import (
    AreaProperties,
    PropertiesReply,
    SystemInformation,
    SystemStatus,
    UnitProperties,
    ZoneProperties,
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
