"""Typed system-event objects for Omni-Link II push notifications.

The panel batches state-change notifications into a single ``SystemEvents``
message (v2 opcode 55). Each batched event is a single 16-bit big-endian
word in the message payload — the message envelope is just a sequence of
those words. The 16-bit value is *category-encoded*: the high bits pick
the event family (zone, unit, arming, alarm, AC, battery, …) and the
remaining bits carry per-family fields (zone index, area, alarm type,
unit number, security mode, etc.).

Pipeline:

    raw bytes   ->  Message (opcode 55)
    Message     ->  parse_events(message) -> list[SystemEvent]
    SystemEvent ->  ZoneStateChanged | UnitStateChanged | ArmingChanged | …

A single SystemEvents message can carry multiple events, so the public
parse entry points always return a *list*. ``EventStream`` flattens that
list across an underlying ``OmniConnection.unsolicited()`` iterator so
consumers can iterate one typed event at a time.

References (decompiled C# source):
    clsOLMsgSystemEvents.cs            — message envelope + per-event word read
    enuOmniLink2MessageType.cs:60      — SystemEvents = 55 (v2 opcode)
    enuEventType.cs                    — category enum (the values used here)
    enuAlarmType.cs                    — alarm subtype byte
    enuSecurityMode.cs                 — security mode byte (used by arming)
    clsText.cs:1585-1690 (GetEventCategory)
                                       — bit-mask classifier we mirror below
    clsText.cs:1693-1911 (GetEventText)
                                       — per-category sub-field extraction

Cross-references (HAI OmniPro II Installation Manual):
    APPENDIX A — CONTACT ID REPORTING FORMAT (p68): the Contact ID
        event codes the panel transmits to a central monitoring station
        for each :class:`AlarmKind`. The class names below mirror those
        codes one-for-one. (pca-re/docs/manuals/installation_manual/
        10_APPENDIX_A_CONTACT_ID_REPORTING_FORMAT/)
    APPENDIX B — DIGITAL COMMUNICATOR CODE SHEET (p69-73): the 4/2 and
        3/1 reporting-format code tables. Useful when correlating a
        SystemEvents word with what a central station would see. (12_…)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import IntEnum
from typing import ClassVar

from .message import Message
from .models import SecurityMode
from .opcodes import OmniLink2MessageType

# --------------------------------------------------------------------------
# Numeric tags / enums
# --------------------------------------------------------------------------


class EventType(IntEnum):
    """Symbolic identifiers for the event subclasses we expose.

    These are *not* the raw 16-bit on-the-wire codes — those are densely
    packed bit fields. We assign each typed-event subclass a stable small
    integer so that ``SystemEvent.event_type`` is a single discriminator
    and ``EVENT_REGISTRY`` can dispatch on it.

    Reference: enuEventType.cs (the C# equivalent that drives clsText
    .GetEventCategory). The order/values below intentionally mirror the
    classification order in clsText.cs:1585-1690 so a future maintainer
    reading both files side-by-side sees the same shape.
    """

    USER_MACRO_BUTTON = 0          # clsText.cs:1587-1590
    PRO_LINK_MESSAGE = 1           # clsText.cs:1591-1594
    CENTRALITE_SWITCH = 2          # clsText.cs:1595-1598
    ALARM_ACTIVATED = 3            # clsText.cs:1599-1602, 1738-1750
    ALARM_CLEARED = 4              # synthesized: ALARM word with alarm_type=0
    ZONE_STATE_CHANGED = 5         # clsText.cs:1603-1606, 1751-1756
    UNIT_STATE_CHANGED = 6         # clsText.cs:1607-1610, 1757-1765
    X10_CODE = 7                   # clsText.cs:1615-1618
    ALL_ON_OFF = 8                 # clsText.cs:1643-1646
    PHONE_LINE_DEAD = 9            # clsText.cs:1649,1853-1857
    PHONE_LINE_RING = 10           # clsText.cs:1651,1858-1859
    PHONE_LINE_OFF_HOOK = 11       # clsText.cs:1653,1860-1861
    PHONE_LINE_ON_HOOK = 12        # clsText.cs:1655,1862-1863
    AC_LOST = 13                   # clsText.cs:1657,1866-1870
    AC_RESTORED = 14               # clsText.cs:1659,1871-1872
    BATTERY_LOW = 15               # clsText.cs:1661,1875-1879
    BATTERY_RESTORED = 16          # clsText.cs:1663,1880-1881
    DCM_TROUBLE = 17               # clsText.cs:1665,1884-1888
    DCM_OK = 18                    # clsText.cs:1667,1889-1890
    ENERGY_COST_LOW = 19           # clsText.cs:1669,1893-1897
    ENERGY_COST_MID = 20           # clsText.cs:1671,1898-1899
    ENERGY_COST_HIGH = 21          # clsText.cs:1673,1900-1901
    ENERGY_COST_CRITICAL = 22      # clsText.cs:1675,1902-1903
    CAMERA = 23                    # clsText.cs:1677-1683,1906-1907
    ACCESS_READER = 24             # clsText.cs:1684-1688,1908-1909
    UPB_LINK = 25                  # clsText.cs:1635-1638,1795-1810
    ARMING_CHANGED = 26            # clsText.cs:1689,2140-2217 (catch-all)
    UNKNOWN = 0xFF                 # parser couldn't classify


class AlarmKind(IntEnum):
    """Alarm subtype byte (enuAlarmType.cs)."""

    ANY = 0                  # enuAlarmType.cs:5
    BURGLARY = 1             # enuAlarmType.cs:6
    FIRE = 2                 # enuAlarmType.cs:7
    GAS = 3                  # enuAlarmType.cs:8
    AUX = 4                  # enuAlarmType.cs:9
    FREEZE = 5               # enuAlarmType.cs:10
    WATER = 6                # enuAlarmType.cs:11
    DURESS = 7               # enuAlarmType.cs:12
    TEMPERATURE = 8          # enuAlarmType.cs:13
    CONFIRMED_BURGLARY = 9   # enuAlarmType.cs:14


class UpbLinkAction(IntEnum):
    """UPB link sub-action, the upper byte of a UPB-LINK event word.

    The C# code maps these via ``enuButtonType`` — UPBLinkOff/On/Set/
    FadeStop — and the enum values are picked so they line up with the
    on-the-wire upper byte (clsText.cs:1801-1808 + enuEventType.cs:14-19,
    where UPB_LINK_OFF=64512, UPB_LINK_ON=64768, UPB_LINK_SET=65024,
    UPB_LINK_FADE_STOP=65280 — i.e. high-byte = 0xFC, 0xFD, 0xFE, 0xFF).
    """

    OFF = 0xFC
    ON = 0xFD
    SET = 0xFE
    FADE_STOP = 0xFF


# --------------------------------------------------------------------------
# Wire-format helpers
# --------------------------------------------------------------------------


def _ensure_system_events(
    message: Message,
    expected_opcode: int = int(OmniLink2MessageType.SystemEvents),
) -> bytes:
    """Validate that ``message`` is a SystemEvents reply, return payload bytes.

    The v1 and v2 SystemEvents inner-message bodies are byte-identical
    (clsOLMsgSystemEvents.cs vs clsOL2MsgSystemEvents.cs both yield
    ``[opcode][word1_hi][word1_lo][word2_hi][word2_lo]…``); only the
    opcode byte differs (35 vs 55). Pass ``expected_opcode`` to dispatch
    the v1 path from :class:`omni_pca.v1.adapter.OmniClientV1Adapter`.
    """
    if message.opcode != expected_opcode:
        raise ValueError(
            f"not a SystemEvents message: opcode {message.opcode} "
            f"(expected {expected_opcode})"
        )
    payload = message.payload
    if len(payload) % 2 != 0:
        # The C# count formula is ``(MessageLength - 1) / 2`` and silently
        # truncates a trailing odd byte. We do the same — never raise.
        payload = payload[: len(payload) - 1]
    return payload


def _iter_event_words(payload: bytes) -> list[int]:
    """Split a SystemEvents payload into 16-bit BE words.

    Reference: clsOLMsgSystemEvents.cs:15-18 (SystemEvent(index) accessor).
    """
    return [(payload[i] << 8) | payload[i + 1] for i in range(0, len(payload), 2)]


# --------------------------------------------------------------------------
# Base + concrete event classes
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemEvent:
    """Base class for every typed system-event object.

    Subclasses override ``EVENT_TYPE`` (the discriminator) and may add
    extra attributes carrying decoded fields. The original 16-bit word
    is always preserved as ``raw_word`` so callers can fall back to the
    unstructured value for diagnostics.
    """

    event_type: EventType
    raw_word: int
    EVENT_TYPE: ClassVar[EventType | None] = None

    @classmethod
    def parse(cls, message: Message) -> list[SystemEvent]:
        """Decode every event word in a v2 SystemEvents message.

        Returns a list because a single message can batch multiple events
        (clsOLMsgSystemEvents.SystemEventsCount() — one count value, but
        the protocol allows ``count`` to be > 1).
        """
        return parse_events(message)


@dataclass(frozen=True, slots=True)
class UserMacroButton(SystemEvent):
    """A user macro button (1-255) was triggered.

    Wire layout: ``(word & 0xFF00) == 0`` → button index in low byte.
    Reference: clsText.cs:1587-1590, 1697-1698.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.USER_MACRO_BUTTON
    button_index: int = 0


@dataclass(frozen=True, slots=True)
class ProLinkMessage(SystemEvent):
    """A Pro-Link message (256..383) was received.

    Wire layout: ``(word & 0xFF80) == 0x100`` → message index in low 7 bits.
    Reference: clsText.cs:1591-1594, 1699-1700.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.PRO_LINK_MESSAGE
    message_index: int = 0


@dataclass(frozen=True, slots=True)
class CentraLiteSwitch(SystemEvent):
    """A CentraLite/Aegis scene-keypad button was pressed.

    Wire layout: ``(word & 0xFF80) == 0x180`` → switch sub-index in low 7 bits.
    Reference: clsText.cs:1595-1598, 1701-1736.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.CENTRALITE_SWITCH
    switch_index: int = 0


@dataclass(frozen=True, slots=True)
class AlarmActivated(SystemEvent):
    """A real alarm condition was triggered.

    Wire layout: ``(word & 0xFF00) == 0x200`` (the ALARM family).
        - bits 4-7 of low byte (``(word & 0xF0) >> 4``) → enuAlarmType
        - bits 0-3 of low byte (``word & 0xF``)         → area index
          (0 means "system-wide alarm, no specific area")
    Reference: clsText.cs:1599-1602, 1738-1750.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ALARM_ACTIVATED
    area_index: int = 0
    alarm_type: int = 0  # AlarmKind value


@dataclass(frozen=True, slots=True)
class AlarmCleared(SystemEvent):
    """An alarm condition was cleared.

    Synthesized — the wire word is in the ALARM family but with the
    alarm-type nibble equal to ``AlarmKind.ANY`` (0). The C# code does
    not have a separate cleared category; it simply formats the word as
    an "Any" alarm. We split it out so home-automation callers can react
    to "alarm went away" without rebuilding the bitfield themselves.

    Reference: clsText.cs:1738-1750 (the ``a`` variable can be 0).
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ALARM_CLEARED
    area_index: int = 0


@dataclass(frozen=True, slots=True)
class ZoneStateChanged(SystemEvent):
    """A security zone changed state (open/close, secure/not-ready).

    Wire layout: ``(word & 0xFC00) == 0x400`` (ZONE_STATE_CHANGE family).
        - low byte                         → zone index 1..255
        - bit 9 (``(word >> 8) & 0x02``)   → 1 = not-ready/open, 0 = secure
    Reference: clsText.cs:1603-1606, 1751-1756.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ZONE_STATE_CHANGED
    zone_index: int = 0
    new_state: int = 0  # 0=secure, 1=not-ready/open

    @property
    def is_open(self) -> bool:
        return self.new_state != 0

    @property
    def is_secure(self) -> bool:
        return self.new_state == 0


@dataclass(frozen=True, slots=True)
class UnitStateChanged(SystemEvent):
    """A controllable unit (light/output) changed state.

    Wire layout: ``(word & 0xFC00) == 0x800`` (UNIT_STATE_CHANGE family).
        - unit index = ``((word >> 8) & 1) * 256 + (word & 0xFF)``
          (so bit 8 lifts the index above 255)
        - bit 9 (``(word >> 8) & 0x02``)     → 1 = on, 0 = off
    Reference: clsText.cs:1607-1610, 1757-1765.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.UNIT_STATE_CHANGED
    unit_index: int = 0
    new_state: int = 0  # 0=off, 1=on

    @property
    def is_on(self) -> bool:
        return self.new_state != 0


@dataclass(frozen=True, slots=True)
class X10CodeReceived(SystemEvent):
    """An X-10 house/unit code was seen on the powerline.

    Wire layout: ``(word & 0xFC00) == 0xC00``.
        - house letter A..P     = chr(65 + ((word & 0xFF) >> 4))
        - unit number 1..16     = (word & 0xF) + 1
        - on/off (bit 9)        = ``((word >> 8) & 2) == 2`` → On
        - "all units" (bit 8)   = ``(word & 0x100) != 0``
    Reference: clsText.cs:1615-1618, 1785-1793.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.X10_CODE
    house_code: str = "A"
    unit_number: int = 1
    is_on: bool = False
    all_units: bool = False


@dataclass(frozen=True, slots=True)
class AllOnOff(SystemEvent):
    """An "All On" or "All Off" command was issued.

    Wire layout: ``(word & 0xFFE0) == 0x3E0`` (=992 family).
        - bit 4 (``(word & 0x10) >> 4``)  → 0 = all off, 1 = all on
        - low 4 bits (``word & 0xF``)     → area (0 = system-wide)
    Reference: clsText.cs:1643-1646, 1835-1851.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ALL_ON_OFF
    area_index: int = 0
    on: bool = False


@dataclass(frozen=True, slots=True)
class PhoneLineDead(SystemEvent):
    """Phone line went dead (word == 768).

    Reference: clsText.cs:1649, 1853-1857."""

    EVENT_TYPE: ClassVar[EventType] = EventType.PHONE_LINE_DEAD


@dataclass(frozen=True, slots=True)
class PhoneLineRinging(SystemEvent):
    """Phone is ringing (word == 769).

    Reference: clsText.cs:1651, 1858-1859."""

    EVENT_TYPE: ClassVar[EventType] = EventType.PHONE_LINE_RING


@dataclass(frozen=True, slots=True)
class PhoneLineOffHook(SystemEvent):
    """Panel went off-hook to dial out (word == 770).

    Reference: clsText.cs:1653, 1860-1861."""

    EVENT_TYPE: ClassVar[EventType] = EventType.PHONE_LINE_OFF_HOOK


@dataclass(frozen=True, slots=True)
class PhoneLineOnHook(SystemEvent):
    """Panel hung up (word == 771).

    Reference: clsText.cs:1655, 1862-1863."""

    EVENT_TYPE: ClassVar[EventType] = EventType.PHONE_LINE_ON_HOOK


@dataclass(frozen=True, slots=True)
class AcLost(SystemEvent):
    """Mains AC was lost (word == 772).

    Reference: clsText.cs:1657, 1866-1870."""

    EVENT_TYPE: ClassVar[EventType] = EventType.AC_LOST


@dataclass(frozen=True, slots=True)
class AcRestored(SystemEvent):
    """Mains AC came back (word == 773).

    Reference: clsText.cs:1659, 1871-1872."""

    EVENT_TYPE: ClassVar[EventType] = EventType.AC_RESTORED


@dataclass(frozen=True, slots=True)
class BatteryLow(SystemEvent):
    """Backup battery is low (word == 774).

    Reference: clsText.cs:1661, 1875-1879."""

    EVENT_TYPE: ClassVar[EventType] = EventType.BATTERY_LOW


@dataclass(frozen=True, slots=True)
class BatteryRestored(SystemEvent):
    """Backup battery is OK again (word == 775).

    Reference: clsText.cs:1663, 1880-1881."""

    EVENT_TYPE: ClassVar[EventType] = EventType.BATTERY_RESTORED


@dataclass(frozen=True, slots=True)
class DcmTrouble(SystemEvent):
    """Digital communicator failure (word == 776).

    Reference: clsText.cs:1665, 1884-1888."""

    EVENT_TYPE: ClassVar[EventType] = EventType.DCM_TROUBLE


@dataclass(frozen=True, slots=True)
class DcmOk(SystemEvent):
    """Digital communicator OK (word == 777).

    Reference: clsText.cs:1667, 1889-1890."""

    EVENT_TYPE: ClassVar[EventType] = EventType.DCM_OK


@dataclass(frozen=True, slots=True)
class EnergyCostChanged(SystemEvent):
    """Real-time energy-cost band changed (words 778..781).

    The discriminator on the dataclass tells you which band; ``raw_word``
    keeps the original number in case a future firmware adds more.
    Reference: clsText.cs:1669-1676, 1893-1903.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ENERGY_COST_LOW  # placeholder, overridden by parse
    cost_level: int = 0  # 0=low, 1=mid, 2=high, 3=critical


@dataclass(frozen=True, slots=True)
class CameraTrigger(SystemEvent):
    """A camera input fired (words 782..787).

    Wire layout: ``camera_index = word - 781`` → 1..6.
    Reference: clsText.cs:1677-1683, 1906-1907.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.CAMERA
    camera_index: int = 1


@dataclass(frozen=True, slots=True)
class AccessReaderEvent(SystemEvent):
    """An access-control reader emitted an event (words 976..991).

    Wire layout: ``reader_index = (word & 0xF) + 1``.
    Reference: clsText.cs:1684-1688, 1908-1909.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ACCESS_READER
    reader_index: int = 1


@dataclass(frozen=True, slots=True)
class UpbLinkEvent(SystemEvent):
    """A UPB link command was sent (words 0xFC00..0xFFFF).

    Wire layout: ``(word & 0xFC00) == 0xFC00``.
        - upper byte → enuButtonType: UPBLinkOff(0xFC), UPBLinkOn(0xFD),
          UPBLinkSet(0xFE), UPBLinkFadeStop(0xFF)
        - lower byte → link index 1..255
    Reference: clsText.cs:1635-1638, 1795-1810.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.UPB_LINK
    link_index: int = 0
    action: int = 0  # UpbLinkAction value (raw upper byte)


@dataclass(frozen=True, slots=True)
class ArmingChanged(SystemEvent):
    """An area's security mode changed (catch-all SECURITY_MODE_CHANGE).

    Wire layout (clsText.cs:2155-2217 — this is the default branch of
    ``GetButtonText``, which is what GetEventCategory routes to when the
    event word doesn't match any other family):

        - bits 12-14 (``(word >> 12) & 7``)  → enuSecurityMode value
        - bits 8-11  (``(word >> 8) & 0xF``) → area index (0 = system)
        - low byte   (``word & 0xFF``)       → user/code index that
          triggered the change (0 = unknown / panel-initiated)
        - bit 15 (``(word >> 8) & 0x80``)    → "Set" vs. "Arm" verb,
          surfaced as ``is_set_command``
        - bit 11 (``(word >> 8) & 0x40``)... reserved-ish; left as raw
    Reference: clsText.cs:1689 (catch-all) + 2140-2217 (decoder).
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.ARMING_CHANGED
    area_index: int = 0
    new_mode: int = 0  # SecurityMode value
    user_index: int = 0
    is_set_command: bool = False  # True for SET (panic/Lumina), False for ARM

    @property
    def mode_name(self) -> str:
        try:
            return SecurityMode(self.new_mode).name
        except ValueError:
            return f"Unknown({self.new_mode})"


@dataclass(frozen=True, slots=True)
class UnknownEvent(SystemEvent):
    """Catch-all so an unrecognised event word never crashes the iterator.

    The event type byte was parseable but didn't match any family in
    clsText.GetEventCategory's classification — likely a future-firmware
    code we haven't mapped yet.
    """

    EVENT_TYPE: ClassVar[EventType] = EventType.UNKNOWN


# --------------------------------------------------------------------------
# Per-word classifier (mirrors clsText.GetEventCategory)
# --------------------------------------------------------------------------


def _classify(word: int) -> SystemEvent:
    """Decode a single 16-bit event word into the appropriate subclass.

    Mirrors ``clsText.GetEventCategory`` (clsText.cs:1585-1690) and the
    per-family field-extraction in ``GetEventText`` (clsText.cs:1693-1911).
    The classification order matters — exact-match cases (PHONE_, AC_,
    BATTERY_, …) are inspected before the wide SECURITY_MODE_CHANGE
    catch-all, exactly as the C# does.
    """
    # USER_MACRO_BUTTON: high byte == 0
    if (word & 0xFF00) == 0x0000:
        return UserMacroButton(
            event_type=EventType.USER_MACRO_BUTTON,
            raw_word=word,
            button_index=word & 0xFF,
        )

    # PRO_LINK_MESSAGE: high 9 bits == 0x100
    if (word & 0xFF80) == 0x0100:
        return ProLinkMessage(
            event_type=EventType.PRO_LINK_MESSAGE,
            raw_word=word,
            message_index=word & 0x7F,
        )

    # CENTRALITE_SWITCH: high 9 bits == 0x180
    if (word & 0xFF80) == 0x0180:
        return CentraLiteSwitch(
            event_type=EventType.CENTRALITE_SWITCH,
            raw_word=word,
            switch_index=word & 0x7F,
        )

    # ALARM (activated/cleared): top byte == 0x02
    if (word & 0xFF00) == 0x0200:
        alarm_type = (word & 0xF0) >> 4
        area = word & 0x0F
        if alarm_type == int(AlarmKind.ANY):
            # Per clsText.cs:1738-1750, the "ANY" subtype is what the panel
            # emits when an alarm is being cleared — it formats it as
            # "Any alarm cleared" string. Surface as a distinct subclass.
            return AlarmCleared(
                event_type=EventType.ALARM_CLEARED,
                raw_word=word,
                area_index=area,
            )
        return AlarmActivated(
            event_type=EventType.ALARM_ACTIVATED,
            raw_word=word,
            area_index=area,
            alarm_type=alarm_type,
        )

    # ZONE_STATE_CHANGE: top 6 bits == 0x4
    if (word & 0xFC00) == 0x0400:
        return ZoneStateChanged(
            event_type=EventType.ZONE_STATE_CHANGED,
            raw_word=word,
            zone_index=word & 0xFF,
            new_state=1 if ((word >> 8) & 0x02) == 0x02 else 0,
        )

    # UNIT_STATE_CHANGE: top 6 bits == 0x8
    if (word & 0xFC00) == 0x0800:
        unit_index = ((word >> 8) & 0x01) * 256 + (word & 0xFF)
        return UnitStateChanged(
            event_type=EventType.UNIT_STATE_CHANGED,
            raw_word=word,
            unit_index=unit_index,
            new_state=1 if ((word >> 8) & 0x02) == 0x02 else 0,
        )

    # X-10 code: top 6 bits == 0xC
    if (word & 0xFC00) == 0x0C00:
        return X10CodeReceived(
            event_type=EventType.X10_CODE,
            raw_word=word,
            house_code=chr(65 + ((word & 0xFF) >> 4)),
            unit_number=(word & 0x0F) + 1,
            is_on=((word >> 8) & 0x02) == 0x02,
            all_units=(word & 0x0100) != 0,
        )

    # ALL_ON_OFF: top 11 bits == 992 (0x3E0) — covers 992..1023, but
    # we leave 1024+ for ZONE which has already been handled above.
    if (word & 0xFFE0) == 0x03E0:
        return AllOnOff(
            event_type=EventType.ALL_ON_OFF,
            raw_word=word,
            area_index=word & 0x0F,
            on=(word & 0x10) != 0,
        )

    # Exact-match singletons (PHONE_, AC_, BATTERY_, DCM_, ENERGY, CAMERA,
    # ACCESS_READER) come before the SECURITY catch-all.
    if word == 768:
        return PhoneLineDead(event_type=EventType.PHONE_LINE_DEAD, raw_word=word)
    if word == 769:
        return PhoneLineRinging(event_type=EventType.PHONE_LINE_RING, raw_word=word)
    if word == 770:
        return PhoneLineOffHook(event_type=EventType.PHONE_LINE_OFF_HOOK, raw_word=word)
    if word == 771:
        return PhoneLineOnHook(event_type=EventType.PHONE_LINE_ON_HOOK, raw_word=word)
    if word == 772:
        return AcLost(event_type=EventType.AC_LOST, raw_word=word)
    if word == 773:
        return AcRestored(event_type=EventType.AC_RESTORED, raw_word=word)
    if word == 774:
        return BatteryLow(event_type=EventType.BATTERY_LOW, raw_word=word)
    if word == 775:
        return BatteryRestored(event_type=EventType.BATTERY_RESTORED, raw_word=word)
    if word == 776:
        return DcmTrouble(event_type=EventType.DCM_TROUBLE, raw_word=word)
    if word == 777:
        return DcmOk(event_type=EventType.DCM_OK, raw_word=word)
    if 778 <= word <= 781:
        level = word - 778
        return EnergyCostChanged(
            event_type=EventType(EventType.ENERGY_COST_LOW + level),
            raw_word=word,
            cost_level=level,
        )
    if 782 <= word <= 787:
        return CameraTrigger(
            event_type=EventType.CAMERA,
            raw_word=word,
            camera_index=word - 781,
        )
    if 976 <= word <= 991:
        return AccessReaderEvent(
            event_type=EventType.ACCESS_READER,
            raw_word=word,
            reader_index=(word & 0x0F) + 1,
        )

    # UPB_LINK: top 6 bits == 0xFC (covers 0xFC00..0xFFFF).
    # The C# code peels off the 0xFD00 (UPB_LINK_ON) sub-family first to
    # check whether the unit is actually an HLC or Z-Wave room controller
    # (clsText.cs:1619-1633). We don't have access to the panel's unit
    # type cache here, so we always classify these as UpbLinkEvent — the
    # caller can refine using the unit_index if they care.
    if (word & 0xFC00) == 0xFC00:
        upper = (word >> 8) & 0xFF
        return UpbLinkEvent(
            event_type=EventType.UPB_LINK,
            raw_word=word,
            link_index=word & 0xFF,
            action=upper,
        )

    # SECURITY_MODE_CHANGE catch-all (clsText.cs:1689). This is the
    # default branch in GetEventCategory: anything that didn't match any
    # of the families above lands here. The 16-bit layout is:
    #     bits 12-14  → SecurityMode (0..7)
    #     bits 8-11   → area index   (0 = system / no specific area)
    #     bit  15     → "Set" vs. "Arm" verb  (Lumina vs. Omni semantics)
    #     low byte    → user/code index that triggered the change
    if (word >> 8) & 0xF0:
        # Plausible arming change: the high nibble of the high byte is
        # non-zero (carries either the Set bit or the area+mode bits).
        return ArmingChanged(
            event_type=EventType.ARMING_CHANGED,
            raw_word=word,
            area_index=(word >> 8) & 0x0F,
            new_mode=(word >> 12) & 0x07,
            user_index=word & 0xFF,
            is_set_command=((word >> 8) & 0x80) == 0x80,
        )

    return UnknownEvent(event_type=EventType.UNKNOWN, raw_word=word)


# --------------------------------------------------------------------------
# Public parse entry points
# --------------------------------------------------------------------------


def parse_events(
    message: Message,
    expected_opcode: int = int(OmniLink2MessageType.SystemEvents),
) -> list[SystemEvent]:
    """Decode a ``SystemEvents`` message into typed events.

    The panel batches multiple state changes into a single message, so
    the return type is always a list — even for messages that carry just
    one event. Empty SystemEvents messages return an empty list rather
    than raising.

    ``expected_opcode`` defaults to v2 (55); pass v1's value (35) when
    decoding from a ``v1.OmniConnectionV1`` push stream.

    Reference: clsOLMsgSystemEvents.cs / clsOL2MsgSystemEvents.cs.
    """
    payload = _ensure_system_events(message, expected_opcode)
    return [_classify(w) for w in _iter_event_words(payload)]


# --------------------------------------------------------------------------
# Registry — discriminator → subclass, useful for callers doing isinstance
# routing or generating documentation.
# --------------------------------------------------------------------------

EVENT_REGISTRY: dict[int, type[SystemEvent]] = {
    int(EventType.USER_MACRO_BUTTON): UserMacroButton,
    int(EventType.PRO_LINK_MESSAGE): ProLinkMessage,
    int(EventType.CENTRALITE_SWITCH): CentraLiteSwitch,
    int(EventType.ALARM_ACTIVATED): AlarmActivated,
    int(EventType.ALARM_CLEARED): AlarmCleared,
    int(EventType.ZONE_STATE_CHANGED): ZoneStateChanged,
    int(EventType.UNIT_STATE_CHANGED): UnitStateChanged,
    int(EventType.X10_CODE): X10CodeReceived,
    int(EventType.ALL_ON_OFF): AllOnOff,
    int(EventType.PHONE_LINE_DEAD): PhoneLineDead,
    int(EventType.PHONE_LINE_RING): PhoneLineRinging,
    int(EventType.PHONE_LINE_OFF_HOOK): PhoneLineOffHook,
    int(EventType.PHONE_LINE_ON_HOOK): PhoneLineOnHook,
    int(EventType.AC_LOST): AcLost,
    int(EventType.AC_RESTORED): AcRestored,
    int(EventType.BATTERY_LOW): BatteryLow,
    int(EventType.BATTERY_RESTORED): BatteryRestored,
    int(EventType.DCM_TROUBLE): DcmTrouble,
    int(EventType.DCM_OK): DcmOk,
    int(EventType.ENERGY_COST_LOW): EnergyCostChanged,
    int(EventType.ENERGY_COST_MID): EnergyCostChanged,
    int(EventType.ENERGY_COST_HIGH): EnergyCostChanged,
    int(EventType.ENERGY_COST_CRITICAL): EnergyCostChanged,
    int(EventType.CAMERA): CameraTrigger,
    int(EventType.ACCESS_READER): AccessReaderEvent,
    int(EventType.UPB_LINK): UpbLinkEvent,
    int(EventType.ARMING_CHANGED): ArmingChanged,
    int(EventType.UNKNOWN): UnknownEvent,
}


# --------------------------------------------------------------------------
# Helper: queue-backed iterator for tests + library consumers
# --------------------------------------------------------------------------


def _has_unsolicited(obj: object) -> bool:
    """True if ``obj`` quacks like an OmniConnection (has ``unsolicited()``).

    We avoid importing OmniConnection at runtime to keep the dependency
    purely a type hint, so EventStream stays usable with any object that
    exposes the same async-iterator contract (real connection, mock, or
    in-memory queue wrapper used in tests).
    """
    return callable(getattr(obj, "unsolicited", None))


@dataclass
class EventStream:
    """Async iterator over typed ``SystemEvent`` objects.

    Wraps any object with an ``unsolicited() -> AsyncIterator[Message]``
    method (typically an :class:`OmniConnection`). Filters out non-
    SystemEvents messages, parses each SystemEvents message into a list
    of typed events, and yields them one at a time. A single inbound
    message that batches three events therefore produces three iterator
    steps — callers don't have to know about batching.

    Usage::

        async for event in EventStream(conn):
            match event:
                case ZoneStateChanged() if event.is_open:
                    print(f"zone {event.zone_index} opened")
                case ArmingChanged():
                    print(f"area {event.area_index} -> {event.mode_name}")
    """

    source: object  # OmniConnection or duck-typed equivalent
    expected_opcode: int = int(OmniLink2MessageType.SystemEvents)
    _buffer: list[SystemEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not _has_unsolicited(self.source):
            raise TypeError(
                "EventStream source must expose an unsolicited() method "
                f"(got {type(self.source).__name__})"
            )

    def __aiter__(self) -> EventStream:
        return self

    async def __anext__(self) -> SystemEvent:
        # Drain buffered events from the previous batched message first.
        while not self._buffer:
            try:
                # ``unsolicited()`` returns a fresh async generator each
                # call on the real connection, but tests pass us a queue
                # wrapper that returns a long-lived iterator. Either way
                # we want one message at a time, so we manually advance.
                if not hasattr(self, "_iter") or self._iter is None:  # type: ignore[has-type]
                    self._iter = self.source.unsolicited().__aiter__()  # type: ignore[attr-defined]
                msg = await self._iter.__anext__()
            except StopAsyncIteration:
                raise
            except asyncio.CancelledError:
                raise
            if msg.opcode != self.expected_opcode:
                # Non-event message (Status, Ack, …) — silently ignore.
                continue
            self._buffer = parse_events(msg, self.expected_opcode)
        return self._buffer.pop(0)


__all__ = [
    "EVENT_REGISTRY",
    "AcLost",
    "AcRestored",
    "AccessReaderEvent",
    "AlarmActivated",
    "AlarmCleared",
    "AlarmKind",
    "AllOnOff",
    "ArmingChanged",
    "BatteryLow",
    "BatteryRestored",
    "CameraTrigger",
    "CentraLiteSwitch",
    "DcmOk",
    "DcmTrouble",
    "EnergyCostChanged",
    "EventStream",
    "EventType",
    "PhoneLineDead",
    "PhoneLineOffHook",
    "PhoneLineOnHook",
    "PhoneLineRinging",
    "ProLinkMessage",
    "SystemEvent",
    "UnitStateChanged",
    "UnknownEvent",
    "UpbLinkAction",
    "UpbLinkEvent",
    "UserMacroButton",
    "X10CodeReceived",
    "ZoneStateChanged",
    "parse_events",
]
