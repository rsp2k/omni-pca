"""Tests for ``omni_pca.events`` — typed system-event parsing.

The test data is hand-built — each helper constructs a synthetic v2
``SystemEvents`` (opcode 55) ``Message`` containing one or more 16-bit
event words encoded in the panel's wire layout. Cross-reference the
bit-mask comments below against ``clsText.GetEventCategory``
(clsText.cs:1585-1690) and ``GetEventText`` (clsText.cs:1693-1911).
"""

from __future__ import annotations

import asyncio

import pytest

from omni_pca.events import (
    EVENT_REGISTRY,
    AccessReaderEvent,
    AcLost,
    AcRestored,
    AlarmActivated,
    AlarmCleared,
    AlarmKind,
    AllOnOff,
    ArmingChanged,
    BatteryLow,
    BatteryRestored,
    CameraTrigger,
    DcmOk,
    DcmTrouble,
    EnergyCostChanged,
    EventStream,
    EventType,
    PhoneLineDead,
    PhoneLineOffHook,
    PhoneLineOnHook,
    PhoneLineRinging,
    SystemEvent,
    UnitStateChanged,
    UnknownEvent,
    UpbLinkAction,
    UpbLinkEvent,
    UserMacroButton,
    X10CodeReceived,
    ZoneStateChanged,
    parse_events,
)
from omni_pca.message import START_CHAR_V2, Message
from omni_pca.opcodes import OmniLink2MessageType

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_events_message(*words: int) -> Message:
    """Build a v2 SystemEvents (opcode 55) Message containing the given
    16-bit event words, encoded big-endian on the wire."""
    payload = bytearray()
    for w in words:
        payload.append((w >> 8) & 0xFF)
        payload.append(w & 0xFF)
    data = bytes([int(OmniLink2MessageType.SystemEvents)]) + bytes(payload)
    return Message(start_char=START_CHAR_V2, data=data)


# --------------------------------------------------------------------------
# Per-subclass parse tests — one event per message
# --------------------------------------------------------------------------


def test_parse_user_macro_button() -> None:
    msg = _make_events_message(0x0042)
    events = parse_events(msg)
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, UserMacroButton)
    assert ev.button_index == 0x42
    assert ev.event_type is EventType.USER_MACRO_BUTTON
    assert ev.raw_word == 0x0042


def test_parse_alarm_activated_burglary_area_3() -> None:
    # ALARM family = 0x0200; (alarm_type=Burglary=1) << 4 | area=3 = 0x13
    word = 0x0200 | (int(AlarmKind.BURGLARY) << 4) | 0x03
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, AlarmActivated)
    assert ev.alarm_type == AlarmKind.BURGLARY
    assert ev.area_index == 3


def test_parse_alarm_cleared_when_alarm_type_zero() -> None:
    # ALARM family with alarm_type=ANY(0): we surface as a cleared event.
    word = 0x0200 | (int(AlarmKind.ANY) << 4) | 0x05
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, AlarmCleared)
    assert ev.area_index == 5


def test_parse_zone_state_changed_open() -> None:
    # ZONE family = 0x0400; bit 9 (0x0200) set ⇒ not-ready/open; zone 17.
    word = 0x0400 | 0x0200 | 17
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, ZoneStateChanged)
    assert ev.zone_index == 17
    assert ev.is_open
    assert not ev.is_secure
    assert ev.new_state == 1


def test_parse_zone_state_changed_secure() -> None:
    word = 0x0400 | 23  # bit 9 clear ⇒ secure
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, ZoneStateChanged)
    assert ev.zone_index == 23
    assert ev.is_secure


def test_parse_unit_state_changed_high_index_on() -> None:
    # UNIT family = 0x0800; index 300 = bit 8 set + low byte 44; bit 9 = on.
    # 300 = 256 + 44; bit 8 of high byte == ((1<<8))=0x100; OR bit 9 (0x200).
    word = 0x0800 | 0x0200 | 0x0100 | 44  # high-byte bit 0 (=0x100) + bit 1 (=0x200)
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, UnitStateChanged)
    assert ev.unit_index == 300
    assert ev.is_on
    assert ev.new_state == 1


def test_parse_unit_state_changed_low_index_off() -> None:
    word = 0x0800 | 7  # bit 9 clear, no index extension
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, UnitStateChanged)
    assert ev.unit_index == 7
    assert not ev.is_on


def test_parse_x10_code_received() -> None:
    # X-10 family = 0x0C00; house 'B' (=1<<4 in low byte high nibble),
    # unit 5 (=4 in low nibble, +1), bit 9 ⇒ on.
    word = 0x0C00 | 0x0200 | (1 << 4) | 4
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, X10CodeReceived)
    assert ev.house_code == "B"
    assert ev.unit_number == 5
    assert ev.is_on
    assert not ev.all_units


def test_parse_all_on_off_area_2_on() -> None:
    # ALL_ON_OFF family = 0x03E0; area 2 in low nibble; on bit (0x10) set.
    word = 0x03E0 | 0x10 | 2
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, AllOnOff)
    assert ev.area_index == 2
    assert ev.on


def test_parse_phone_singletons() -> None:
    cases: list[tuple[int, type]] = [
        (768, PhoneLineDead),
        (769, PhoneLineRinging),
        (770, PhoneLineOffHook),
        (771, PhoneLineOnHook),
    ]
    for word, klass in cases:
        [ev] = parse_events(_make_events_message(word))
        assert isinstance(ev, klass), (word, type(ev))
        assert ev.raw_word == word


def test_parse_ac_battery_dcm_singletons() -> None:
    [ac_off] = parse_events(_make_events_message(772))
    [ac_on] = parse_events(_make_events_message(773))
    [batt_low] = parse_events(_make_events_message(774))
    [batt_ok] = parse_events(_make_events_message(775))
    [dcm_bad] = parse_events(_make_events_message(776))
    [dcm_ok] = parse_events(_make_events_message(777))
    assert isinstance(ac_off, AcLost)
    assert isinstance(ac_on, AcRestored)
    assert isinstance(batt_low, BatteryLow)
    assert isinstance(batt_ok, BatteryRestored)
    assert isinstance(dcm_bad, DcmTrouble)
    assert isinstance(dcm_ok, DcmOk)


def test_parse_energy_cost_levels() -> None:
    for word, level in [(778, 0), (779, 1), (780, 2), (781, 3)]:
        [ev] = parse_events(_make_events_message(word))
        assert isinstance(ev, EnergyCostChanged)
        assert ev.cost_level == level


def test_parse_camera_trigger() -> None:
    [ev] = parse_events(_make_events_message(785))  # 785 - 781 = 4 → camera 4
    assert isinstance(ev, CameraTrigger)
    assert ev.camera_index == 4


def test_parse_access_reader_event() -> None:
    # 976..991: reader_index = (word & 0xF) + 1
    [ev] = parse_events(_make_events_message(978))
    assert isinstance(ev, AccessReaderEvent)
    assert ev.reader_index == ((978 & 0xF) + 1)


def test_parse_upb_link_actions() -> None:
    # UPB_LINK family = 0xFC00; upper byte selects action.
    actions = [
        (UpbLinkAction.OFF, 0xFC),
        (UpbLinkAction.ON, 0xFD),
        (UpbLinkAction.SET, 0xFE),
        (UpbLinkAction.FADE_STOP, 0xFF),
    ]
    for action, upper in actions:
        word = (upper << 8) | 12  # link index 12
        [ev] = parse_events(_make_events_message(word))
        assert isinstance(ev, UpbLinkEvent), (action, ev)
        assert ev.link_index == 12
        assert ev.action == int(action)


def test_parse_arming_changed_user_5_area_2_away() -> None:
    # SECURITY_MODE_CHANGE catch-all:
    #   bits 12-14 = SecurityMode (3 = AWAY)
    #   bits 8-11  = area (2)
    #   low byte   = user/code (5)
    word = (3 << 12) | (2 << 8) | 5
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, ArmingChanged)
    assert ev.area_index == 2
    assert ev.new_mode == 3
    assert ev.user_index == 5
    assert ev.mode_name == "AWAY"


def test_parse_arming_changed_set_command_bit() -> None:
    # Same as above but with the "Set" verb bit (bit 15) set.
    word = (1 << 12) | (1 << 15) | (1 << 8) | 9
    [ev] = parse_events(_make_events_message(word))
    assert isinstance(ev, ArmingChanged)
    assert ev.is_set_command
    assert ev.user_index == 9


def test_unknown_event_returned_for_unmapped_word() -> None:
    # Any value in the gap between the special singletons and the SECURITY
    # catch-all that ALSO has zero in the high nibble of the high byte
    # falls through to UnknownEvent. word=900 is in the gap (after CAMERA
    # range, before ACCESS_READER) and (900 >> 8) & 0xF0 = 0 → unknown.
    [ev] = parse_events(_make_events_message(900))
    assert isinstance(ev, UnknownEvent)
    assert ev.event_type is EventType.UNKNOWN
    assert ev.raw_word == 900


# --------------------------------------------------------------------------
# Multi-event-per-packet (the panel batches into a single SystemEvents msg)
# --------------------------------------------------------------------------


def test_parse_three_events_in_one_message() -> None:
    msg = _make_events_message(
        0x0400 | 0x0200 | 5,                               # zone 5 opened
        (3 << 12) | (1 << 8) | 7,                          # area 1 → AWAY by user 7
        773,                                                # AC restored
    )
    events = parse_events(msg)
    assert len(events) == 3
    z, a, ac = events
    assert isinstance(z, ZoneStateChanged)
    assert z.zone_index == 5
    assert z.is_open
    assert isinstance(a, ArmingChanged)
    assert a.area_index == 1
    assert a.new_mode == 3
    assert isinstance(ac, AcRestored)


def test_empty_system_events_message_returns_empty_list() -> None:
    msg = _make_events_message()  # zero event words
    assert parse_events(msg) == []


def test_odd_trailing_byte_is_silently_truncated() -> None:
    """The C# count is ``(MessageLength - 1) / 2`` — a trailing odd byte
    is dropped, not raised. We mirror that to stay tolerant of messages
    where the panel has appended a stray byte (seen on some firmwares)."""
    data = bytes([int(OmniLink2MessageType.SystemEvents)]) + b"\x00\x42\x77"
    msg = Message(start_char=START_CHAR_V2, data=data)
    events = parse_events(msg)
    assert len(events) == 1
    assert isinstance(events[0], UserMacroButton)
    assert events[0].button_index == 0x42


def test_parse_rejects_wrong_opcode() -> None:
    # opcode 25 = SystemStatus, not SystemEvents.
    msg = Message(
        start_char=START_CHAR_V2,
        data=bytes([int(OmniLink2MessageType.SystemStatus)]) + b"\x00",
    )
    with pytest.raises(ValueError, match="not a SystemEvents message"):
        parse_events(msg)


def test_classmethod_parse_matches_function() -> None:
    msg = _make_events_message(0x0042, 773)
    via_classmethod = SystemEvent.parse(msg)
    via_function = parse_events(msg)
    assert [type(e) for e in via_classmethod] == [type(e) for e in via_function]
    assert [e.raw_word for e in via_classmethod] == [e.raw_word for e in via_function]


# --------------------------------------------------------------------------
# Registry sanity check
# --------------------------------------------------------------------------


def test_event_registry_covers_every_eventtype_value() -> None:
    """Every non-UNKNOWN EventType value should map to a concrete class."""
    for et in EventType:
        assert int(et) in EVENT_REGISTRY, f"missing registry entry for {et!r}"


# --------------------------------------------------------------------------
# EventStream — async iterator over an underlying connection-like source
# --------------------------------------------------------------------------


class _FakeConnection:
    """Minimal stand-in for OmniConnection used in the EventStream tests.

    Exposes the same ``unsolicited() -> AsyncIterator[Message]`` contract
    backed by an in-memory queue so the test harness can drive the stream
    deterministically without touching real I/O.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Message] = asyncio.Queue()
        self._closed = False

    def push(self, msg: Message) -> None:
        self.queue.put_nowait(msg)

    def close(self) -> None:
        self._closed = True

    def unsolicited(self):
        async def _gen():
            while True:
                if self._closed and self.queue.empty():
                    return
                msg = await self.queue.get()
                yield msg
        return _gen()


@pytest.mark.asyncio
async def test_event_stream_yields_one_typed_event_per_step() -> None:
    conn = _FakeConnection()
    # Three events spread across two messages — confirms both flattening
    # and cross-message iteration work.
    conn.push(_make_events_message(0x0042, 773))           # 2 events
    conn.push(_make_events_message(0x0400 | 0x0200 | 9))   # 1 event
    conn.close()

    stream = EventStream(source=conn)
    seen: list[SystemEvent] = []
    async for ev in stream:
        seen.append(ev)
        if len(seen) == 3:
            break

    assert isinstance(seen[0], UserMacroButton)
    assert seen[0].button_index == 0x42
    assert isinstance(seen[1], AcRestored)
    assert isinstance(seen[2], ZoneStateChanged)
    assert seen[2].zone_index == 9
    assert seen[2].is_open


@pytest.mark.asyncio
async def test_event_stream_skips_non_event_messages() -> None:
    """Status replies, Acks, etc. that show up on the unsolicited
    channel must be silently dropped — only opcode 55 produces events."""
    conn = _FakeConnection()
    # Inject a SystemStatus reply (opcode 25) — should be filtered out.
    other = Message(
        start_char=START_CHAR_V2,
        data=bytes([int(OmniLink2MessageType.SystemStatus)]) + b"\x00" * 14,
    )
    conn.push(other)
    conn.push(_make_events_message(0x0042))
    conn.close()

    stream = EventStream(source=conn)
    ev = await stream.__anext__()
    assert isinstance(ev, UserMacroButton)
    assert ev.button_index == 0x42


def test_event_stream_rejects_non_connection_source() -> None:
    with pytest.raises(TypeError, match="unsolicited"):
        EventStream(source=object())  # type: ignore[arg-type]
