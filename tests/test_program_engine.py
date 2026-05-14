"""Tests for the autonomous program execution engine.

Phase 1 coverage: Clock abstraction, program classification, engine
lifecycle. Subsequent phases add tests as they land.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from omni_pca.mock_panel import MockPanel, MockState
from omni_pca.program_engine import (
    Clock,
    FakeClock,
    ProgramEngine,
    RealClock,
    classify,
)
from omni_pca.programs import (
    CondArgType,
    CondOP,
    Days,
    Program,
    ProgramType,
)

CONTROLLER_KEY = bytes(range(16))


# ---- Clock ---------------------------------------------------------------


def test_real_clock_now_is_utc_aware() -> None:
    c = RealClock()
    assert c.now().tzinfo is not None


@pytest.mark.asyncio
async def test_real_clock_sleep_until_past_returns_immediately() -> None:
    c = RealClock()
    past = c.now() - timedelta(seconds=10)
    # Should not actually sleep.
    await asyncio.wait_for(c.sleep_until(past), timeout=0.5)


@pytest.mark.asyncio
async def test_real_clock_sleep_until_short_future() -> None:
    c = RealClock()
    target = c.now() + timedelta(milliseconds=50)
    await c.sleep_until(target)
    assert c.now() >= target


def test_fake_clock_requires_tz_aware_initial() -> None:
    with pytest.raises(ValueError):
        FakeClock(datetime(2026, 5, 14, 12, 0))  # no tz


def test_fake_clock_now_returns_set_time() -> None:
    t0 = datetime(2026, 5, 14, 22, 30, tzinfo=timezone.utc)
    c = FakeClock(t0)
    assert c.now() == t0


@pytest.mark.asyncio
async def test_fake_clock_advance_wakes_sleepers() -> None:
    t0 = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    c = FakeClock(t0)
    target = t0 + timedelta(minutes=30)

    woke: list[datetime] = []

    async def sleeper() -> None:
        await c.sleep_until(target)
        woke.append(c.now())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)  # let sleeper register
    assert woke == []
    await c.advance_to(target)
    await task
    assert len(woke) == 1
    assert woke[0] == target


@pytest.mark.asyncio
async def test_fake_clock_advance_wakes_in_chronological_order() -> None:
    t0 = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    c = FakeClock(t0)
    woke: list[tuple[str, datetime]] = []

    async def sleeper(label: str, delta_min: int) -> None:
        await c.sleep_until(t0 + timedelta(minutes=delta_min))
        woke.append((label, c.now()))

    s1 = asyncio.create_task(sleeper("late", 60))
    s2 = asyncio.create_task(sleeper("early", 15))
    s3 = asyncio.create_task(sleeper("middle", 30))
    await asyncio.sleep(0)
    # Jump past everything in one go.
    await c.advance_to(t0 + timedelta(minutes=90))
    await s1
    await s2
    await s3
    assert [label for label, _ in woke] == ["early", "middle", "late"]


def test_fake_clock_cannot_move_backwards() -> None:
    t0 = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    c = FakeClock(t0)
    with pytest.raises(ValueError):
        # advance_to is async but the validation is synchronous.
        asyncio.run(c.advance_to(t0 - timedelta(seconds=1)))


def test_clock_is_abstract() -> None:
    with pytest.raises(TypeError):
        Clock()  # type: ignore[abstract]


# ---- Classification ------------------------------------------------------


def _free() -> Program:
    return Program(slot=1, prog_type=int(ProgramType.FREE))


def _timed(slot: int) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.TIMED),
        cmd=3, hour=6, minute=0, days=int(Days.MONDAY),
    )


def _event(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.EVENT), cmd=5, cond=0x0001)


def _yearly(slot: int) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.YEARLY),
        cmd=4, month=5, day=14, hour=12, minute=0,
    )


def _bare_when(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.WHEN), cond=0x0001)


def _bare_and(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.AND))


def _bare_then(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.THEN), cmd=3)


def test_classify_buckets_each_type() -> None:
    bag = (
        _free(), _timed(2), _event(3), _yearly(4),
        _bare_when(5), _bare_and(6), _bare_then(7),
    )
    out = classify(bag)
    assert [p.slot for p in out.timed] == [2]
    assert [p.slot for p in out.event] == [3]
    assert [p.slot for p in out.yearly] == [4]
    assert [p.slot for p in out.clausal_heads] == [5]
    # FREE, AND, THEN are not in any bucket.


def test_classify_drops_unknown_prog_types() -> None:
    # Use a raw int that isn't a valid ProgramType.
    junk = Program(slot=1, prog_type=99)
    out = classify((junk,))
    assert out.timed == ()
    assert out.event == ()
    assert out.yearly == ()
    assert out.clausal_heads == ()


def test_classify_handles_empty_input() -> None:
    out = classify(())
    assert out.timed == ()
    assert out.event == ()
    assert out.yearly == ()
    assert out.clausal_heads == ()


# ---- Engine lifecycle ----------------------------------------------------


def _panel_with_programs(*programs: Program) -> MockPanel:
    return MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(
            programs={p.slot: p.encode_wire_bytes() for p in programs if p.slot},
        ),
    )


@pytest.mark.asyncio
async def test_engine_constructs_against_empty_panel() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    assert engine.running is False
    assert engine.classified.timed == ()


@pytest.mark.asyncio
async def test_engine_classifies_loaded_programs() -> None:
    panel = _panel_with_programs(_timed(1), _event(2), _yearly(3), _bare_when(4))
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    assert len(engine.classified.timed) == 1
    assert len(engine.classified.event) == 1
    assert len(engine.classified.yearly) == 1
    assert len(engine.classified.clausal_heads) == 1


@pytest.mark.asyncio
async def test_engine_start_stop_idempotent() -> None:
    panel = _panel_with_programs(_timed(1))
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    await engine.start()
    assert engine.running
    await engine.start()  # idempotent
    assert engine.running
    await engine.stop()
    assert not engine.running
    await engine.stop()  # idempotent
    assert not engine.running


@pytest.mark.asyncio
async def test_engine_context_manager() -> None:
    panel = _panel_with_programs(_timed(1))
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    async with engine:
        assert engine.running
    assert not engine.running


@pytest.mark.asyncio
async def test_engine_defaults_to_real_clock() -> None:
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    engine = ProgramEngine(panel)
    assert isinstance(engine.clock, RealClock)


@pytest.mark.asyncio
async def test_engine_skips_malformed_records() -> None:
    """Garbage in panel.state.programs shouldn't break engine construction."""
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        # Half-length blob — too short for from_wire_bytes; should be skipped.
        state=MockState(programs={1: b"\x01\x02\x03"}),
    )
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    # No tasks spawned, no exceptions raised, just empty classification.
    assert engine.classified.timed == ()


# ---- Phase 2: TIMED execution -------------------------------------------


from omni_pca.commands import Command  # noqa: E402
from omni_pca.program_engine import (  # noqa: E402
    _matches_days_mask,
    _next_absolute_fire,
)


def test_matches_days_mask_empty_never_matches() -> None:
    assert _matches_days_mask(date(2026, 5, 14), 0) is False


def test_matches_days_mask_monday() -> None:
    # 2026-05-11 is a Monday.
    assert _matches_days_mask(date(2026, 5, 11), int(Days.MONDAY)) is True
    assert _matches_days_mask(date(2026, 5, 12), int(Days.MONDAY)) is False


def test_matches_days_mask_weekdays_combo() -> None:
    weekdays = int(Days.MONDAY | Days.TUESDAY | Days.WEDNESDAY | Days.THURSDAY | Days.FRIDAY)
    # Mon..Fri match; Sat (5/16) / Sun (5/17) don't.
    for day in (11, 12, 13, 14, 15):
        assert _matches_days_mask(date(2026, 5, day), weekdays)
    for day in (16, 17):
        assert not _matches_days_mask(date(2026, 5, day), weekdays)


def test_next_absolute_fire_today_future() -> None:
    # 2026-05-14 Thu 22:00; program 22:30 weekdays → fires today 22:30.
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        hour=22, minute=30,
        days=int(Days.THURSDAY),
    )
    now = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    nxt = _next_absolute_fire(now, p)
    assert nxt == datetime(2026, 5, 14, 22, 30, tzinfo=timezone.utc)


def test_next_absolute_fire_today_already_past_rolls_forward() -> None:
    # 23:00 Thu, program 22:30 Thu → next is next Thursday.
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        hour=22, minute=30,
        days=int(Days.THURSDAY),
    )
    now = datetime(2026, 5, 14, 23, 0, tzinfo=timezone.utc)
    nxt = _next_absolute_fire(now, p)
    assert nxt == datetime(2026, 5, 21, 22, 30, tzinfo=timezone.utc)


def test_next_absolute_fire_no_days_returns_none() -> None:
    p = Program(slot=1, prog_type=int(ProgramType.TIMED), hour=6, minute=0, days=0)
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    assert _next_absolute_fire(now, p) is None


@pytest.mark.asyncio
async def test_engine_fires_timed_program_at_scheduled_time() -> None:
    """End-to-end: TIMED UNIT_ON program at 06:00 Mon fires when the
    fake clock advances past Monday 06:00 and mutates MockUnitState."""
    t0 = datetime(2026, 5, 11, 5, 59, tzinfo=timezone.utc)  # Mon 05:59
    fire_at = datetime(2026, 5, 11, 6, 0, tzinfo=timezone.utc)
    after = datetime(2026, 5, 11, 6, 1, tzinfo=timezone.utc)
    # Unit 7 OFF initially; program turns it ON.
    p = Program(
        slot=42, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0,
        days=int(Days.MONDAY),
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        # Let the worker schedule itself.
        await asyncio.sleep(0)
        await clock.advance_to(after)
        # Yield so the worker can finish firing.
        await asyncio.sleep(0)
        assert engine.metrics.timed_fired == 1
        assert panel.state.units[7].state == 1  # ON


@pytest.mark.asyncio
async def test_engine_fires_timed_program_repeatedly() -> None:
    """Loop-around: same Monday program fires again the next Monday."""
    t0 = datetime(2026, 5, 11, 5, 59, tzinfo=timezone.utc)  # Mon 05:59
    p = Program(
        slot=42, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0,
        days=int(Days.MONDAY),
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        # Walk the clock forward week-by-week so each Monday's fire
        # completes (advance_to wakes one sleeper at a time; the worker
        # needs to re-register for the next week between advances).
        for week in range(1, 3):
            await clock.advance_to(t0 + timedelta(days=7 * week))
            await asyncio.sleep(0)
        assert engine.metrics.timed_fired == 2


@pytest.mark.asyncio
async def test_engine_does_not_fire_outside_days_mask() -> None:
    """A Mon-only program does NOT fire if the clock only advances on Tue."""
    t0 = datetime(2026, 5, 12, 5, 59, tzinfo=timezone.utc)  # Tue 05:59
    p = Program(
        slot=42, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0,
        days=int(Days.MONDAY),
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        # Advance only ~6 hours — Tuesday 12:00 — still before next Monday 06:00.
        await clock.advance_to(t0 + timedelta(hours=6))
        await asyncio.sleep(0)
        assert engine.metrics.timed_fired == 0
        # And the unit is still OFF.
        assert 7 not in panel.state.units or panel.state.units[7].state == 0


@pytest.mark.asyncio
async def test_engine_skips_empty_days_mask() -> None:
    """A program with no Days set never fires — matches real panel."""
    t0 = datetime(2026, 5, 11, 5, 59, tzinfo=timezone.utc)
    p = Program(
        slot=42, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0,
        days=0,  # disabled
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        await clock.advance_to(t0 + timedelta(days=7))
        await asyncio.sleep(0)
        assert engine.metrics.timed_fired == 0


# ---- Phase 3: YEARLY + sunrise/sunset -----------------------------------


from omni_pca.program_engine import (  # noqa: E402
    PanelLocation,
    _next_sun_relative_fire,
    _next_yearly_fire,
)


def test_panel_location_from_account_negates_longitude() -> None:
    """The Omni stores longitude as positive degrees west; PanelLocation
    flips the sign to match astral's east-positive convention."""

    class _AcctStub:
        latitude = 44
        longitude = 117
        time_zone = 7

    loc = PanelLocation.from_account(_AcctStub())
    assert loc.latitude == 44.0
    assert loc.longitude == -117.0  # flipped from +117 west → -117 east
    assert loc.timezone == "Etc/GMT+7"


def test_next_yearly_fire_picks_today() -> None:
    # 2026-05-14 12:00; program 05/14 13:00 → today 13:00.
    p = Program(
        slot=1, prog_type=int(ProgramType.YEARLY),
        cmd=1, month=5, day=14, hour=13, minute=0,
    )
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    nxt = _next_yearly_fire(now, p)
    assert nxt == datetime(2026, 5, 14, 13, 0, tzinfo=timezone.utc)


def test_next_yearly_fire_rolls_over_to_next_year() -> None:
    # 2026-12-31 23:00; program 01/01 00:00 → next year.
    p = Program(
        slot=1, prog_type=int(ProgramType.YEARLY),
        cmd=1, month=1, day=1, hour=0, minute=0,
    )
    now = datetime(2026, 12, 31, 23, 0, tzinfo=timezone.utc)
    nxt = _next_yearly_fire(now, p)
    assert nxt == datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc)


def test_next_yearly_fire_zero_month_returns_none() -> None:
    p = Program(
        slot=1, prog_type=int(ProgramType.YEARLY),
        cmd=1, month=0, day=0, hour=0, minute=0,
    )
    now = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    assert _next_yearly_fire(now, p) is None


def test_next_yearly_fire_invalid_date_returns_none() -> None:
    """Feb 30 is invalid — program never fires."""
    p = Program(
        slot=1, prog_type=int(ProgramType.YEARLY),
        cmd=1, month=2, day=30, hour=12, minute=0,
    )
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert _next_yearly_fire(now, p) is None


@pytest.mark.asyncio
async def test_engine_fires_yearly_program() -> None:
    """YEARLY May-14 12:00 program fires when clock crosses that date."""
    t0 = datetime(2026, 5, 14, 11, 59, tzinfo=timezone.utc)
    p = Program(
        slot=10, prog_type=int(ProgramType.YEARLY),
        cmd=int(Command.UNIT_ON), pr2=3,
        month=5, day=14, hour=12, minute=0,
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        await clock.advance_to(datetime(2026, 5, 14, 12, 1, tzinfo=timezone.utc))
        await asyncio.sleep(0)
        assert engine.metrics.yearly_fired == 1
        assert panel.state.units[3].state == 1


@pytest.mark.asyncio
async def test_engine_yearly_loops_next_year() -> None:
    """After firing, YEARLY workers re-arm for the next year."""
    t0 = datetime(2026, 5, 14, 11, 59, tzinfo=timezone.utc)
    p = Program(
        slot=10, prog_type=int(ProgramType.YEARLY),
        cmd=int(Command.UNIT_ON), pr2=3,
        month=5, day=14, hour=12, minute=0,
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        # First fire.
        await clock.advance_to(datetime(2026, 5, 14, 12, 1, tzinfo=timezone.utc))
        await asyncio.sleep(0)
        # Second fire next year.
        await clock.advance_to(datetime(2027, 5, 14, 12, 1, tzinfo=timezone.utc))
        await asyncio.sleep(0)
        assert engine.metrics.yearly_fired == 2


def test_next_sun_relative_fire_at_sunset() -> None:
    """A TIMED program scheduled "at sunset" on a Thursday fires at
    the astral-computed sunset for that day."""
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=5,
        hour=26, minute=0,  # AT_SUNSET sentinel
        days=int(Days.THURSDAY),
    )
    loc = PanelLocation(
        name="Boise", region="US", timezone="UTC",
        latitude=43.6, longitude=-116.2,
    )
    # 2026-05-14 is a Thursday.
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    nxt = _next_sun_relative_fire(now, p, loc)
    assert nxt is not None
    # Sunset in Boise mid-May is roughly 03:00 UTC the *next* day (late
    # evening Mountain Time). Just verify it's in the right ballpark
    # and after `now`.
    assert nxt > now
    assert nxt < now + timedelta(days=2)


def test_next_sun_relative_fire_with_offset_before_sunrise() -> None:
    """A "30 min before sunrise" program lands earlier than astral's
    raw sunrise time by exactly 30 minutes."""
    p_at = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=1, hour=25, minute=0,  # AT_SUNRISE
        days=int(Days.THURSDAY),
    )
    p_before = Program(
        slot=2, prog_type=int(ProgramType.TIMED),
        cmd=1, hour=25, minute=256 - 30,  # 30 min before (sbyte -30)
        days=int(Days.THURSDAY),
    )
    loc = PanelLocation(
        name="Boise", region="US", timezone="UTC",
        latitude=43.6, longitude=-116.2,
    )
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    at = _next_sun_relative_fire(now, p_at, loc)
    before = _next_sun_relative_fire(now, p_before, loc)
    assert at is not None and before is not None
    # The "before" fire is 30 minutes earlier than the "at" fire.
    assert at - before == timedelta(minutes=30)


def test_next_sun_relative_fire_empty_days_returns_none() -> None:
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=1, hour=25, minute=0,
        days=0,  # disabled
    )
    loc = PanelLocation(latitude=43.6, longitude=-116.2)
    now = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    assert _next_sun_relative_fire(now, p, loc) is None


@pytest.mark.asyncio
async def test_engine_sun_relative_without_location_is_skipped() -> None:
    """Engine with no PanelLocation drops sunrise/sunset programs."""
    t0 = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=5,
        hour=25, minute=0,  # AT_SUNRISE
        days=int(Days.THURSDAY),
    )
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        # No location → worker returns immediately, never fires.
        await asyncio.sleep(0)
        await clock.advance_to(t0 + timedelta(days=2))
        await asyncio.sleep(0)
        assert engine.metrics.timed_fired == 0


# ---- Phase 4: EVENT programs --------------------------------------------


from omni_pca.program_engine import (  # noqa: E402
    EVENT_AC_POWER_OFF,
    event_id_unit_state,
    event_id_user_macro_button,
    event_id_zone_state,
)


def test_event_id_user_macro_button_packs_low_byte() -> None:
    assert event_id_user_macro_button(1) == 0x0001
    assert event_id_user_macro_button(255) == 0x00FF


def test_event_id_user_macro_button_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        event_id_user_macro_button(0)
    with pytest.raises(ValueError):
        event_id_user_macro_button(256)


def test_event_id_zone_state_encodes_zone_and_state() -> None:
    # Zone 1 state 0 (secure) → 0x0400 base.
    assert event_id_zone_state(1, 0) == 0x0400
    assert event_id_zone_state(1, 3) == 0x0403  # tamper
    # Zone 2 state 0 = 0x0404 (2-1)*4+0
    assert event_id_zone_state(2, 0) == 0x0404


def test_event_id_unit_state_encodes_unit_and_on_off() -> None:
    assert event_id_unit_state(1, on=False) == 0x0800
    assert event_id_unit_state(1, on=True) == 0x0801
    assert event_id_unit_state(2, on=True) == 0x0803


@pytest.mark.asyncio
async def test_engine_emit_event_fires_subscribed_program() -> None:
    """An EVENT program with event_id matching the emit fires."""
    button_evt = event_id_user_macro_button(5)
    # EVENT program stores event_id in (month<<8)|day per programs.event_id.
    p = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=7,
        month=(button_evt >> 8) & 0xFF, day=button_evt & 0xFF,
    )
    panel = _panel_with_programs(p)
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        fired = await engine.emit_user_macro_button(5)
        assert fired == 1
        assert engine.metrics.event_fired == 1
        assert panel.state.units[7].state == 1


@pytest.mark.asyncio
async def test_engine_emit_event_no_match_returns_zero() -> None:
    """Emitting an event with no subscribed program is a silent no-op."""
    panel = MockPanel(controller_key=CONTROLLER_KEY, state=MockState())
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        assert await engine.emit_user_macro_button(99) == 0
        assert engine.metrics.event_fired == 0


@pytest.mark.asyncio
async def test_engine_emit_event_before_start_is_no_op() -> None:
    """emit_event on an un-started engine doesn't raise — just returns 0."""
    p = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=1, month=0, day=1,
    )
    panel = _panel_with_programs(p)
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    assert await engine.emit_event(1) == 0  # not started yet


@pytest.mark.asyncio
async def test_engine_multiple_programs_on_same_event() -> None:
    """Two EVENT programs subscribed to the same event both fire."""
    evt = event_id_user_macro_button(3)
    hi = (evt >> 8) & 0xFF
    lo = evt & 0xFF
    p1 = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=5,
        month=hi, day=lo,
    )
    p2 = Program(
        slot=2, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=6,
        month=hi, day=lo,
    )
    panel = _panel_with_programs(p1, p2)
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        fired = await engine.emit_user_macro_button(3)
        assert fired == 2
        assert panel.state.units[5].state == 1
        assert panel.state.units[6].state == 1


@pytest.mark.asyncio
async def test_engine_zone_state_emit_helper() -> None:
    """emit_zone_state fires programs matching that exact (zone, state)."""
    evt = event_id_zone_state(7, 1)  # zone 7 not-ready
    p = Program(
        slot=10, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=12,
        month=(evt >> 8) & 0xFF, day=evt & 0xFF,
    )
    panel = _panel_with_programs(p)
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        # Wrong state — no fire.
        assert await engine.emit_zone_state(7, 0) == 0
        assert engine.metrics.event_fired == 0
        # Right state — fire.
        assert await engine.emit_zone_state(7, 1) == 1
        assert engine.metrics.event_fired == 1


@pytest.mark.asyncio
async def test_engine_fixed_event_constants() -> None:
    """The hand-rolled fixed-ID events (PHONE/AC_POWER) dispatch correctly."""
    p = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=4,
        month=(EVENT_AC_POWER_OFF >> 8) & 0xFF,
        day=EVENT_AC_POWER_OFF & 0xFF,
    )
    panel = _panel_with_programs(p)
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        fired = await engine.emit_event(EVENT_AC_POWER_OFF)
        assert fired == 1


@pytest.mark.asyncio
async def test_engine_fires_sun_relative_program_with_location() -> None:
    """End-to-end: TIMED AT_SUNSET program fires at astral-computed
    sunset when the clock advances past it."""
    loc = PanelLocation(
        name="Boise", region="US", timezone="UTC",
        latitude=43.6, longitude=-116.2,
    )
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=5,
        hour=26, minute=0,  # AT_SUNSET
        days=int(Days.THURSDAY),
    )
    # Start at midnight UTC on a Thursday.
    t0 = datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    panel = _panel_with_programs(p)
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock, location=loc) as engine:
        await asyncio.sleep(0)
        # Advance well past sunset (which is roughly 03:00 UTC Friday).
        await clock.advance_to(t0 + timedelta(days=2))
        await asyncio.sleep(0)
        assert engine.metrics.timed_fired == 1
        assert panel.state.units[5].state == 1


# ---- Phase 5: Clausal chains --------------------------------------------


from omni_pca.program_engine import (  # noqa: E402
    ClausalChain,
    build_chains,
    evaluate_conditions,
)


def _when(slot: int, event_id: int) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.WHEN),
        month=(event_id >> 8) & 0xFF, day=event_id & 0xFF,
    )


def _at(slot: int, hour: int, minute: int, days: int) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.AT),
        hour=hour, minute=minute, days=days,
    )


def _every(slot: int, interval_sec: int) -> Program:
    # every_interval = ((cond & 0xFF) << 8) | ((cond2 >> 8) & 0xFF)
    cond = (interval_sec >> 8) & 0xFF
    cond2 = (interval_sec & 0xFF) << 8
    return Program(
        slot=slot, prog_type=int(ProgramType.EVERY),
        cond=cond, cond2=cond2,
    )


def _and_cond(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.AND))


def _or_cond(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.OR))


def _then_action(slot: int, cmd: int, pr2: int) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.THEN),
        cmd=cmd, pr2=pr2,
    )


def test_build_chains_simple_when_then() -> None:
    """Minimal chain: WHEN at slot 1, THEN at slot 2."""
    chains = build_chains((
        _when(1, 0x0405),
        _then_action(2, int(Command.UNIT_ON), 7),
    ))
    assert len(chains) == 1
    assert chains[0].head.slot == 1
    assert chains[0].conditions == ()
    assert [a.slot for a in chains[0].actions] == [2]


def test_build_chains_with_and_conditions_and_multiple_actions() -> None:
    chains = build_chains((
        _when(1, 0x0405),
        _and_cond(2),
        _and_cond(3),
        _then_action(4, int(Command.UNIT_ON), 1),
        _then_action(5, int(Command.UNIT_OFF), 2),
    ))
    assert len(chains) == 1
    c = chains[0]
    assert [x.slot for x in c.conditions] == [2, 3]
    assert [a.slot for a in c.actions] == [4, 5]


def test_build_chains_separates_adjacent_chains() -> None:
    chains = build_chains((
        _when(1, 0x0405),
        _then_action(2, 1, 1),
        _at(3, 6, 0, int(Days.MONDAY)),
        _then_action(4, 1, 2),
    ))
    assert [c.head.slot for c in chains] == [1, 3]
    assert chains[0].actions[0].slot == 2
    assert chains[1].actions[0].slot == 4


def test_build_chains_drops_chains_without_then() -> None:
    """A WHEN with no THEN has nothing to fire — skip silently."""
    chains = build_chains((
        _when(1, 0x0405),
        _and_cond(2),
        # no THEN
    ))
    assert chains == ()


def test_build_chains_stops_at_non_clausal_record() -> None:
    """A TIMED record between chains ends the prior chain."""
    timed = Program(
        slot=3, prog_type=int(ProgramType.TIMED),
        cmd=1, hour=6, minute=0, days=int(Days.MONDAY),
    )
    chains = build_chains((
        _when(1, 0x0405),
        _then_action(2, 1, 1),
        timed,
        _when(4, 0x0410),
        _then_action(5, 1, 2),
    ))
    assert [c.head.slot for c in chains] == [1, 4]


def test_evaluate_conditions_empty_is_true() -> None:
    assert evaluate_conditions((), is_satisfied=lambda c: False) is True


def test_evaluate_conditions_all_ands() -> None:
    cs = (_and_cond(1), _and_cond(2))
    assert evaluate_conditions(cs, is_satisfied=lambda c: True) is True
    assert evaluate_conditions(cs, is_satisfied=lambda c: False) is False
    # One fails → whole group fails.
    assert evaluate_conditions(
        cs, is_satisfied=lambda c: c.slot == 1,
    ) is False


def test_evaluate_conditions_or_group_separation() -> None:
    """Two groups via OR: group A (AND only) fails, group B (OR + AND) passes."""
    cs = (
        _and_cond(1),  # group A start
        _and_cond(2),
        _or_cond(3),  # group B start (OR record itself)
        _and_cond(4),
    )
    # Group A: slots 1, 2; Group B: slots 3, 4.
    def is_sat(c):
        return c.slot in (3, 4)  # group B fully passes
    assert evaluate_conditions(cs, is_satisfied=is_sat) is True


@pytest.mark.asyncio
async def test_engine_classifies_clausal_heads() -> None:
    """Engine exposes built chains via the chains property."""
    panel = _panel_with_programs(
        _when(1, 0x0405),
        _then_action(2, int(Command.UNIT_ON), 7),
    )
    engine = ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    ))
    assert len(engine.chains) == 1


@pytest.mark.asyncio
async def test_engine_when_chain_fires_on_event() -> None:
    """A WHEN-headed chain dispatches when emit_event() matches its event."""
    button_evt = event_id_user_macro_button(7)
    panel = _panel_with_programs(
        _when(1, button_evt),
        _then_action(2, int(Command.UNIT_ON), 9),
    )
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        fired = await engine.emit_user_macro_button(7)
        assert fired == 1
        assert engine.metrics.clausal_fired == 1
        assert panel.state.units[9].state == 1


@pytest.mark.asyncio
async def test_engine_when_chain_blocked_by_failing_condition() -> None:
    """Default condition evaluator passes ANDs but fails ORs. A chain
    with one AND condition fires; a chain with an OR-only group doesn't."""
    button_evt = event_id_user_macro_button(7)
    panel = _panel_with_programs(
        _when(1, button_evt),
        _and_cond(2),
        _then_action(3, int(Command.UNIT_ON), 9),
    )
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        # Default evaluator: ANDs pass → chain runs.
        fired = await engine.emit_user_macro_button(7)
        assert fired == 1
        assert panel.state.units[9].state == 1


@pytest.mark.asyncio
async def test_engine_custom_evaluator_can_block_chain() -> None:
    """Replace evaluator with always-False; chain doesn't fire."""
    button_evt = event_id_user_macro_button(7)
    panel = _panel_with_programs(
        _when(1, button_evt),
        _and_cond(2),
        _then_action(3, int(Command.UNIT_ON), 9),
    )
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        engine.set_condition_evaluator(lambda c: False)
        fired = await engine.emit_user_macro_button(7)
        # Returns 0 — the chain matched the event but failed conditions.
        assert fired == 0
        assert engine.metrics.clausal_fired == 0


@pytest.mark.asyncio
async def test_engine_at_chain_fires_at_scheduled_time() -> None:
    """AT-headed chain fires at hour:minute on matching days."""
    t0 = datetime(2026, 5, 11, 5, 59, tzinfo=timezone.utc)  # Mon 05:59
    panel = _panel_with_programs(
        _at(1, hour=6, minute=0, days=int(Days.MONDAY)),
        _then_action(2, int(Command.UNIT_ON), 7),
    )
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        await clock.advance_to(t0 + timedelta(minutes=2))
        await asyncio.sleep(0)
        assert engine.metrics.clausal_fired == 1
        assert panel.state.units[7].state == 1


@pytest.mark.asyncio
async def test_engine_every_chain_fires_on_interval() -> None:
    """EVERY chain fires every N seconds."""
    t0 = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
    panel = _panel_with_programs(
        _every(1, interval_sec=60),
        _then_action(2, int(Command.UNIT_ON), 7),
    )
    clock = FakeClock(t0)
    async with ProgramEngine(panel, clock=clock) as engine:
        await asyncio.sleep(0)
        # Walk three intervals.
        for tick in (1, 2, 3):
            await clock.advance_to(t0 + timedelta(seconds=60 * tick + 1))
            await asyncio.sleep(0)
        assert engine.metrics.clausal_fired == 3


# ---- Phase 6: detailed AND/OR semantics (StateEvaluator) ----------------


from omni_pca.mock_panel import (  # noqa: E402
    MockAreaState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)
from omni_pca.program_engine import StateEvaluator  # noqa: E402


def _and_traditional(
    slot: int, family: int, instance: int = 0,
) -> Program:
    """Build a Traditional (OP=0) AND record.

    Per clsConditionLine.Cond synthesis (clsConditionLine.cs:17-33),
    the family byte lives in disk byte 1 = ``and_family`` (Python's
    ``cond & 0xFF``), and the object instance lives in disk byte 3
    = ``and_instance`` (Python's ``(cond2 >> 8) & 0xFF``).
    """
    return Program(
        slot=slot, prog_type=int(ProgramType.AND),
        cond=family & 0xFF,             # byte 1 = family; byte 2 (OP) = 0
        cond2=(instance & 0xFF) << 8,   # byte 3 = instance; byte 4 = 0
    )


def _and_structured(
    slot: int,
    op: int,
    arg1_type: int, arg1_ix: int, arg1_field: int,
    arg2_type: int, arg2_ix: int, arg2_field: int = 0,
) -> Program:
    """Build a Structured (OP>0) AND record.

    Field layout per programs.py decoders:
      cond high byte (>>8 & 0xFF) = op (and_op)
      cond low byte (& 0xFF) = arg1_argtype (and_arg1_argtype)
      cond2 = arg1_ix (and_arg1_ix)
      cmd = arg1_field (and_arg1_field)
      par = arg2_argtype (and_arg2_argtype)
      pr2 = arg2_ix (and_arg2_ix)
      month = arg2_field (and_arg2_field)
    """
    return Program(
        slot=slot, prog_type=int(ProgramType.AND),
        cond=(op << 8) | arg1_type,
        cond2=arg1_ix,
        cmd=arg1_field,
        par=arg2_type,
        pr2=arg2_ix,
        month=arg2_field,
    )


def _state_with(**kwargs) -> MockState:
    return MockState(**kwargs)


# ---- Traditional ZONE family -------------------------------------------


def test_state_evaluator_zone_secure_passes_when_state_zero() -> None:
    state = _state_with(zones={
        7: MockZoneState(name="FRONT DOOR", current_state=0),
    })
    ev = StateEvaluator(state)
    # ZONE family = 0x04 (secure variant); instance = 7
    cond = _and_traditional(1, family=0x04, instance=7)
    assert ev(cond) is True


def test_state_evaluator_zone_secure_fails_when_tripped() -> None:
    state = _state_with(zones={
        7: MockZoneState(name="FRONT DOOR", current_state=1),  # not-ready
    })
    ev = StateEvaluator(state)
    cond = _and_traditional(1, family=0x04, instance=7)
    assert ev(cond) is False


def test_state_evaluator_zone_not_ready_passes_when_tripped() -> None:
    state = _state_with(zones={
        7: MockZoneState(name="FRONT DOOR", current_state=1),
    })
    ev = StateEvaluator(state)
    # family 0x06 = ZONE + NOT_READY selector bit
    cond = _and_traditional(1, family=0x06, instance=7)
    assert ev(cond) is True


def test_state_evaluator_zone_undefined_is_secure() -> None:
    """Undefined zone reads as SECURE — matches real-panel behaviour
    when a programmed zone slot doesn't exist."""
    ev = StateEvaluator(_state_with())  # no zones
    secure_check = _and_traditional(1, family=0x04, instance=99)
    not_ready_check = _and_traditional(2, family=0x06, instance=99)
    assert ev(secure_check) is True
    assert ev(not_ready_check) is False


# ---- Traditional CTRL family --------------------------------------------


def test_state_evaluator_unit_on_passes_when_state_one() -> None:
    state = _state_with(units={5: MockUnitState(name="LAMP", state=1)})
    ev = StateEvaluator(state)
    # CTRL family + ON selector = 0x0A
    cond = _and_traditional(1, family=0x0A, instance=5)
    assert ev(cond) is True


def test_state_evaluator_unit_off_passes_when_state_zero() -> None:
    state = _state_with(units={5: MockUnitState(name="LAMP", state=0)})
    ev = StateEvaluator(state)
    # CTRL family + OFF selector = 0x08
    cond = _and_traditional(1, family=0x08, instance=5)
    assert ev(cond) is True


def test_state_evaluator_unit_on_for_dimmed_unit() -> None:
    """Dim level 50% → state=150; ON predicate should pass."""
    state = _state_with(units={5: MockUnitState(state=150)})
    ev = StateEvaluator(state)
    cond = _and_traditional(1, family=0x0A, instance=5)
    assert ev(cond) is True


# ---- Traditional SEC family ---------------------------------------------


def test_state_evaluator_security_mode_match() -> None:
    """SEC family: family byte = (mode << 4) | area. Area 1 in mode 2."""
    state = _state_with(areas={1: MockAreaState(name="MAIN", mode=2)})
    ev = StateEvaluator(state)
    cond = _and_traditional(1, family=(2 << 4) | 1)  # mode 2, area 1 = 0x21
    assert ev(cond) is True
    # Area in different mode → fails.
    cond_wrong = _and_traditional(1, family=(3 << 4) | 1)  # mode 3
    assert ev(cond_wrong) is False


# ---- Traditional OTHER family -------------------------------------------


def test_state_evaluator_never_is_always_false() -> None:
    """MiscConditional.NEVER (= 1) → always False, regardless of state."""
    ev = StateEvaluator(_state_with())
    # OTHER family + NEVER misc = 0x01
    cond = _and_traditional(1, family=0x01)
    assert ev(cond) is False


def test_state_evaluator_dark_without_location_is_false() -> None:
    ev = StateEvaluator(_state_with())  # no location
    # OTHER family + DARK misc = 0x03
    cond = _and_traditional(1, family=0x03)
    assert ev(cond) is False


# ---- Structured: Zone fields --------------------------------------------


def test_state_evaluator_structured_zone_current_state_eq() -> None:
    """Zone 5 CurrentState == 1 (not-ready) — structured form."""
    state = _state_with(zones={5: MockZoneState(current_state=1)})
    ev = StateEvaluator(state)
    # EQ, Arg1=ZONE.CurrentState(2), Arg1IX=5, Arg2=CONSTANT, Arg2IX=1
    cond = _and_structured(
        slot=1, op=int(CondOP.ARG1_EQ_ARG2),
        arg1_type=int(CondArgType.ZONE), arg1_ix=5, arg1_field=2,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=1,
    )
    assert ev(cond) is True


def test_state_evaluator_structured_zone_state_ne() -> None:
    state = _state_with(zones={5: MockZoneState(current_state=0)})
    ev = StateEvaluator(state)
    cond = _and_structured(
        slot=1, op=int(CondOP.ARG1_NE_ARG2),
        arg1_type=int(CondArgType.ZONE), arg1_ix=5, arg1_field=2,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=0,
    )
    assert ev(cond) is False  # state IS 0


# ---- Structured: Thermostat fields --------------------------------------


def test_state_evaluator_structured_thermostat_temp_gt() -> None:
    """TEMPERATURE > 75 — structured comparison."""
    # Thermostat raw temperature 168 ~ 76°F on the Omni linear scale,
    # but we compare raw bytes here. Use a raw temp clearly above the
    # constant.
    state = _state_with(thermostats={
        1: MockThermostatState(temperature_raw=80),
    })
    ev = StateEvaluator(state)
    cond = _and_structured(
        slot=1, op=int(CondOP.ARG1_GT_ARG2),
        arg1_type=int(CondArgType.THERMOSTAT), arg1_ix=1, arg1_field=1,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=75,
    )
    assert ev(cond) is True
    # And < should fail.
    cond_lt = _and_structured(
        slot=2, op=int(CondOP.ARG1_LT_ARG2),
        arg1_type=int(CondArgType.THERMOSTAT), arg1_ix=1, arg1_field=1,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=75,
    )
    assert ev(cond_lt) is False


# ---- Structured: TimeDate fields ----------------------------------------


def test_state_evaluator_structured_timedate_hour_compare() -> None:
    """Current hour == 22 — uses the clock."""
    clock = FakeClock(datetime(2026, 5, 14, 22, 30, tzinfo=timezone.utc))
    ev = StateEvaluator(_state_with(), clock=clock)
    cond = _and_structured(
        slot=1, op=int(CondOP.ARG1_EQ_ARG2),
        arg1_type=int(CondArgType.TIME_DATE), arg1_ix=0, arg1_field=8,  # Hour
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=22,
    )
    assert ev(cond) is True


def test_state_evaluator_structured_timedate_dayofweek() -> None:
    """DayOfWeek == 4 (Thursday)."""
    clock = FakeClock(datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc))  # Thu
    ev = StateEvaluator(_state_with(), clock=clock)
    cond = _and_structured(
        slot=1, op=int(CondOP.ARG1_EQ_ARG2),
        arg1_type=int(CondArgType.TIME_DATE), arg1_ix=0, arg1_field=5,  # DOW
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=4,
    )
    assert ev(cond) is True


def test_state_evaluator_structured_timedate_without_clock_is_false() -> None:
    """No clock → TimeDate predicates resolve to None → comparison False."""
    ev = StateEvaluator(_state_with())  # no clock
    cond = _and_structured(
        slot=1, op=int(CondOP.ARG1_EQ_ARG2),
        arg1_type=int(CondArgType.TIME_DATE), arg1_ix=0, arg1_field=8,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=22,
    )
    assert ev(cond) is False


# ---- Engine integration --------------------------------------------------


@pytest.mark.asyncio
async def test_engine_use_state_evaluator_gates_real_conditions() -> None:
    """End-to-end: WHEN + AND IF UNIT 3 ON + THEN UNIT 9 ON.
    Chain fires only when unit 3 is on at the time the event arrives."""
    button_evt = event_id_user_macro_button(5)
    # WHEN button 5; AND IF unit 3 ON; THEN unit 9 ON.
    when = _when(1, button_evt)
    and_cond = _and_traditional(2, family=0x0A, instance=3)  # CTRL + ON, unit 3
    then = _then_action(3, int(Command.UNIT_ON), 9)

    # Start with unit 3 OFF.
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(
            programs={
                1: when.encode_wire_bytes(),
                2: and_cond.encode_wire_bytes(),
                3: then.encode_wire_bytes(),
            },
            units={3: MockUnitState(state=0)},
        ),
    )
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        engine.use_state_evaluator()
        # Unit 3 is OFF — chain blocked.
        fired = await engine.emit_user_macro_button(5)
        assert fired == 0
        assert 9 not in panel.state.units or panel.state.units[9].state == 0

        # Turn unit 3 ON, re-emit — chain fires.
        panel.state.units[3].state = 1
        fired = await engine.emit_user_macro_button(5)
        assert fired == 1
        assert panel.state.units[9].state == 1


@pytest.mark.asyncio
async def test_engine_state_evaluator_or_alternative() -> None:
    """WHEN + AND IF zone 5 secure + OR + AND IF zone 6 secure + THEN.
    Fires if either zone is secure."""
    button_evt = event_id_user_macro_button(5)
    when = _when(1, button_evt)
    and_z5 = _and_traditional(2, family=0x04, instance=5)   # ZONE 5 secure
    or_break = _or_cond(3)                                  # OR boundary
    and_z6 = _and_traditional(4, family=0x04, instance=6)   # ZONE 6 secure
    then = _then_action(5, int(Command.UNIT_ON), 10)

    # Zone 5 tripped, Zone 6 secure → group A fails, group B passes.
    panel = MockPanel(
        controller_key=CONTROLLER_KEY,
        state=MockState(
            programs={
                p.slot: p.encode_wire_bytes()
                for p in (when, and_z5, or_break, and_z6, then)
            },
            zones={
                5: MockZoneState(current_state=1),
                6: MockZoneState(current_state=0),
            },
        ),
    )
    async with ProgramEngine(panel, clock=FakeClock(
        datetime(2026, 5, 14, 0, 0, tzinfo=timezone.utc)
    )) as engine:
        engine.use_state_evaluator()
        fired = await engine.emit_user_macro_button(5)
        assert fired == 1  # OR-alternative passed
        assert panel.state.units[10].state == 1
