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
from omni_pca.programs import Days, Program, ProgramType

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


def _when(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.WHEN), cond=0x0001)


def _and(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.AND))


def _then(slot: int) -> Program:
    return Program(slot=slot, prog_type=int(ProgramType.THEN), cmd=3)


def test_classify_buckets_each_type() -> None:
    bag = (
        _free(), _timed(2), _event(3), _yearly(4), _when(5), _and(6), _then(7),
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
    panel = _panel_with_programs(_timed(1), _event(2), _yearly(3), _when(4))
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
