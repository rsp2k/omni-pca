"""Autonomous execution engine for HAI Omni panel programs.

The :mod:`omni_pca.mock_panel` module turns ``MockState`` into a
wire-speaking *replay* of a panel: clients ask for properties, names,
programs, and the mock serves what's on disk. The engine in this
module is the next layer — it interprets the decoded :class:`Program`
records as **automation rules** and fires them autonomously over time,
mutating ``MockState`` the same way a real panel firmware would.

Architecture
------------

The engine is decoupled from real wall-time via a :class:`Clock`
protocol so tests can fast-forward through schedules without waiting
for real seconds to elapse. Two implementations ship:

* :class:`RealClock` — the production engine. ``now()`` returns
  ``datetime.now()`` and ``sleep_until()`` does ``asyncio.sleep``.
* :class:`FakeClock` — for tests. ``now()`` returns the manually-set
  current time and ``sleep_until()`` returns immediately after
  recording the target. Tests then call ``advance_to(target)`` to
  jump the clock forward and let pending sleepers wake up.

Program-type coverage is built up in phases:

* Phase 1 (this module's initial cut) — skeleton + Clock + the
  classifier that splits :class:`Program` records into "schedulable
  by time", "event-triggered", and "clausal head" categories.
* Phase 2 — TIMED program execution.
* Phase 3 — YEARLY + sunrise/sunset via :mod:`astral`.
* Phase 4 — EVENT program routing.
* Phase 5 — full clausal evaluator for firmware-3.0 multi-record
  WHEN/AT/EVERY + AND/OR/THEN chains.

The engine never touches the wire directly. All state mutations go
through :meth:`MockPanel._apply_unit_command` (and siblings), which
are the same code paths the v2 ``Command`` opcode handler uses — so
"engine fires a TIMED program that turns on unit 5" and "client sends
``Command(UNIT_ON, 5)``" produce identical results.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

from .programs import (
    CondArgType,
    CondOP,
    Days,
    MiscConditional,
    Program,
    ProgramCond,
    ProgramType,
    TimeKind,
)

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .mock_panel import MockPanel


# --------------------------------------------------------------------------
# Clock abstraction
# --------------------------------------------------------------------------


class Clock(ABC):
    """Abstract source of "what time is it" + delay scheduling.

    Implementations decide whether ``sleep_until`` is a real
    ``asyncio.sleep`` or a deterministic no-op that defers to a manual
    advance call. The engine never references ``datetime.now()`` or
    ``asyncio.sleep`` directly — it always goes through the Clock.
    """

    @abstractmethod
    def now(self) -> datetime: ...

    @abstractmethod
    async def sleep_until(self, target: datetime) -> None: ...


class RealClock(Clock):
    """Production clock — wall-time + asyncio.sleep."""

    def now(self) -> datetime:
        return datetime.now(tz=timezone.utc)

    async def sleep_until(self, target: datetime) -> None:
        delay = (target - self.now()).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)


@dataclass
class _PendingSleeper:
    """A coroutine waiting for the FakeClock to reach ``target``."""

    target: datetime
    event: asyncio.Event


class FakeClock(Clock):
    """Deterministic clock for tests.

    ``advance_to(t)`` jumps wall-time forward and wakes any sleepers
    whose ``target <= t``. Multiple sleepers can wait concurrently —
    they wake in target order.

    Example:

        clock = FakeClock(datetime(2026, 5, 14, 22, 29, tzinfo=timezone.utc))
        engine = ProgramEngine(panel, clock=clock)
        await engine.start()
        # No real seconds pass:
        await clock.advance_to(datetime(2026, 5, 14, 22, 31, tzinfo=timezone.utc))
        # By now any TIMED program scheduled for 22:30 has fired.
    """

    def __init__(self, initial: datetime) -> None:
        if initial.tzinfo is None:
            raise ValueError("FakeClock requires a timezone-aware initial datetime")
        self._now = initial
        self._sleepers: list[_PendingSleeper] = []

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, target: datetime) -> None:
        if target <= self._now:
            return
        sleeper = _PendingSleeper(target=target, event=asyncio.Event())
        self._sleepers.append(sleeper)
        await sleeper.event.wait()

    async def advance_to(self, target: datetime) -> None:
        """Jump clock to ``target``, waking any sleepers whose target is in
        the past after the jump. Sleepers wake in chronological order so
        a TIMED program scheduled for 06:00 wakes before one at 07:00
        even if we advance straight to 08:00."""
        if target < self._now:
            raise ValueError("FakeClock can only move forward")
        self._now = target
        # Wake sleepers whose target has now passed, in chronological order.
        ready = sorted(
            (s for s in self._sleepers if s.target <= self._now),
            key=lambda s: s.target,
        )
        for sleeper in ready:
            self._sleepers.remove(sleeper)
            sleeper.event.set()
        # Yield once per ready sleeper so each one's coroutine runs to
        # its next suspension point before we return.
        for _ in ready:
            await asyncio.sleep(0)


# --------------------------------------------------------------------------
# Program classification
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ClassifiedPrograms:
    """Programs sorted into execution buckets.

    ``timed`` / ``event`` / ``yearly`` carry compact-form Programs whose
    behaviour is decoded directly from the single 14-byte record.
    ``clausal_heads`` are WHEN/AT/EVERY records that begin a multi-record
    chain; the engine resolves the chain (via following AND/OR/THEN
    records in the same slot range) when it loads them in phase 5.
    """

    timed: tuple[Program, ...] = ()
    event: tuple[Program, ...] = ()
    yearly: tuple[Program, ...] = ()
    clausal_heads: tuple[Program, ...] = ()


def classify(programs: tuple[Program, ...]) -> _ClassifiedPrograms:
    """Split a Program tuple by execution kind.

    Empty / unknown / FREE / REMARK records are dropped — they have no
    runtime behaviour. Clausal AND/OR/THEN records are *also* dropped at
    this stage; the engine reaches them by walking forward from each
    WHEN/AT/EVERY head, not by classifying them independently.
    """
    timed: list[Program] = []
    event: list[Program] = []
    yearly: list[Program] = []
    clausal: list[Program] = []
    for p in programs:
        if p.is_empty():
            continue
        try:
            kind = ProgramType(p.prog_type)
        except ValueError:
            continue
        if kind == ProgramType.TIMED:
            timed.append(p)
        elif kind == ProgramType.EVENT:
            event.append(p)
        elif kind == ProgramType.YEARLY:
            yearly.append(p)
        elif kind in (ProgramType.WHEN, ProgramType.AT, ProgramType.EVERY):
            clausal.append(p)
        # FREE (0) / REMARK (4) / AND / OR / THEN — not scheduled directly.
    return _ClassifiedPrograms(
        timed=tuple(timed),
        event=tuple(event),
        yearly=tuple(yearly),
        clausal_heads=tuple(clausal),
    )


# --------------------------------------------------------------------------
# Geo / sun events
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PanelLocation:
    """Geographic position for sunrise/sunset computation.

    A thin wrapper that decouples the engine from a hard dependency on
    :mod:`astral`. Build one from a decoded ``.pca`` like::

        from omni_pca.pca_file import parse_pca_file
        acct = parse_pca_file(data, key=KEY_EXPORT)
        loc = PanelLocation.from_account(acct)

    The engine accepts ``location=None`` (the default); in that mode
    sunrise/sunset-relative TIMED programs simply don't fire — equivalent
    to an empty Days mask.
    """

    name: str = "Panel"
    region: str = "US"
    timezone: str = "UTC"
    latitude: float = 0.0
    longitude: float = 0.0

    @classmethod
    def from_account(cls, account, *, name: str = "Panel") -> "PanelLocation":
        """Build a PanelLocation from a :class:`omni_pca.pca_file.PcaAccount`.

        ``acct.latitude``/``longitude`` are raw degrees; ``acct.time_zone``
        is hours west of UTC, which we convert to an IANA-style
        ``"Etc/GMT+N"`` zone — pyttz / astral resolve that correctly.
        (The Etc/GMT signs are inverted relative to common usage by the
        POSIX convention, hence "+N" for west-of-UTC.)
        """
        tz_off = getattr(account, "time_zone", 0)
        # Etc/GMT+0 normalises to UTC for the zero case.
        tz_name = f"Etc/GMT+{tz_off}" if tz_off else "UTC"
        # Longitude on Omni is stored as positive degrees west; astral
        # expects signed degrees east-of-prime. Negate.
        return cls(
            name=name,
            region="US",  # not stored in .pca; "US" is fine as a label
            timezone=tz_name,
            latitude=float(getattr(account, "latitude", 0)),
            longitude=-float(getattr(account, "longitude", 0)),
        )


def _sun_events(location: PanelLocation, day: date) -> tuple[datetime, datetime]:
    """Return (sunrise, sunset) on ``day`` as timezone-aware UTC datetimes.

    Raises :class:`ImportError` if the optional ``astral`` dependency
    isn't installed; callers should catch and treat as "no sun events
    available" (equivalent to skipping the program for that day).
    """
    from astral import LocationInfo
    from astral.sun import sun

    info = LocationInfo(
        name=location.name,
        region=location.region,
        timezone=location.timezone,
        latitude=location.latitude,
        longitude=location.longitude,
    )
    times = sun(info.observer, date=day)
    return times["sunrise"], times["sunset"]


# --------------------------------------------------------------------------
# TIMED scheduling
# --------------------------------------------------------------------------


# Omni's day-of-week bitmask maps bits 1..7 (LSB unused) to Mon..Sun.
# Python's datetime.weekday() returns Mon=0..Sun=6. We need a lookup.
_PYWEEKDAY_TO_DAYS_BIT: tuple[Days, ...] = (
    Days.MONDAY,
    Days.TUESDAY,
    Days.WEDNESDAY,
    Days.THURSDAY,
    Days.FRIDAY,
    Days.SATURDAY,
    Days.SUNDAY,
)


def _matches_days_mask(d: date, mask: int) -> bool:
    """Return True iff ``d``'s weekday is enabled in the Omni Days bitmask.

    Mask 0 (no days set) never matches — TIMED programs with empty
    Days masks are effectively disabled, matching real-panel behaviour.
    """
    if mask == 0:
        return False
    return bool(int(_PYWEEKDAY_TO_DAYS_BIT[d.weekday()]) & mask)


def _next_absolute_fire(now: datetime, program: Program) -> datetime | None:
    """Compute the next datetime ``program`` (assumed TIMED, ABSOLUTE
    TimeKind) should fire, strictly after ``now``.

    Returns ``None`` if the program's Days mask is empty — it never fires.
    """
    if program.time_kind != TimeKind.ABSOLUTE:
        return None  # SUNRISE/SUNSET-relative handled by Phase 3.
    if program.days == 0:
        return None
    # Snap to the program's hour:minute today (in the clock's tz),
    # then walk forward up to 8 days looking for the next matching weekday.
    base = now.replace(
        hour=program.hour, minute=program.minute,
        second=0, microsecond=0,
    )
    for offset in range(0, 8):
        candidate = base + timedelta(days=offset)
        if candidate <= now:
            continue
        if _matches_days_mask(candidate.date(), program.days):
            return candidate
    return None  # safety net — shouldn't happen if mask is non-zero


def _next_sun_relative_fire(
    now: datetime,
    program: Program,
    location: PanelLocation,
) -> datetime | None:
    """Compute next fire for a sunrise/sunset-relative TIMED program.

    For each candidate day (today through +8 days) we compute the
    panel's sunrise/sunset, apply the program's signed minute offset
    (``time_offset_minutes``), and return the first result strictly
    after ``now`` whose date matches the program's Days mask.

    Returns ``None`` if Days mask is empty, the program isn't sun-relative,
    or the astral computation raises.
    """
    if program.time_kind not in (TimeKind.SUNRISE, TimeKind.SUNSET):
        return None
    if program.days == 0:
        return None
    offset = timedelta(minutes=program.time_offset_minutes)
    is_sunrise = program.time_kind == TimeKind.SUNRISE
    for delta_days in range(0, 8):
        day = (now + timedelta(days=delta_days)).date()
        if not _matches_days_mask(day, program.days):
            continue
        try:
            sunrise, sunset = _sun_events(location, day)
        except Exception:
            _log.debug(
                "sun computation failed for %s — skipping day", day, exc_info=True
            )
            return None
        candidate = (sunrise if is_sunrise else sunset) + offset
        if candidate > now:
            return candidate
    return None


def _next_yearly_fire(now: datetime, program: Program) -> datetime | None:
    """Compute the next datetime a YEARLY program should fire.

    YEARLY programs match a fixed month/day at hour:minute, regardless
    of weekday. Returns ``None`` if month is 0 (program disabled) or
    the month/day combination is invalid (e.g. Feb 30).
    """
    if program.month == 0 or program.day == 0:
        return None
    candidate_year = now.year
    for _ in range(2):  # try this year then next
        try:
            candidate = datetime(
                candidate_year, program.month, program.day,
                program.hour, program.minute,
                tzinfo=now.tzinfo,
            )
        except ValueError:
            return None  # Feb 30 etc.
        if candidate > now:
            return candidate
        candidate_year += 1
    return None  # safety net


def _command_payload(program: Program) -> bytes:
    """Build the 4-byte Command wire payload from a Program record.

    The wire format (clsOL2MsgCommand.cs) is identical between the v2
    Command opcode and what the panel firmware fires internally for a
    TIMED program — so feeding this to ``MockPanel._handle_command``
    has exactly the same state-mutation effect as a client sending
    the equivalent Command.
    """
    return bytes(
        [
            program.cmd & 0xFF,
            program.par & 0xFF,
            (program.pr2 >> 8) & 0xFF,
            program.pr2 & 0xFF,
        ]
    )


# --------------------------------------------------------------------------
# Event taxonomy
# --------------------------------------------------------------------------


# Event-ID encoding mirrors clsText.GetEventCategory (clsText.cs:1585-...).
# Each event has a 16-bit ID; bit-pattern masks pick out the category, and
# the low-order bits within each category encode the object number / state.
# We expose helper builders rather than a full enuEventType mirror — the
# common cases below cover what TIMED-program authors actually wire up.


def event_id_user_macro_button(button: int) -> int:
    """Event ID fired when a panel button macro runs.

    Category mask: ``(evt & 0xFF00) == 0x0000``. The low byte holds the
    1-based button number (1..255).
    """
    if not 1 <= button <= 255:
        raise ValueError(f"button {button} out of range 1..255")
    return button & 0xFF


def event_id_zone_state(zone: int, state: int) -> int:
    """Event ID for a zone-state change.

    Category mask: ``(evt & 0xFC00) == 0x0400`` (high bits 0b000001).
    Low 10 bits encode zone × state per clsText: ``(zone - 1) * 4 + state``
    where state is the 2-bit "current_state" code (0=secure, 1=not-ready,
    2=trouble, 3=tamper). Range fits 256 zones × 4 states = 1024 IDs.
    """
    if not 1 <= zone <= 256:
        raise ValueError(f"zone {zone} out of range 1..256")
    if not 0 <= state <= 3:
        raise ValueError(f"state {state} out of range 0..3")
    return 0x0400 | (((zone - 1) * 4 + state) & 0x03FF)


def event_id_unit_state(unit: int, on: bool) -> int:
    """Event ID for a unit (light/output) state change.

    Category mask: ``(evt & 0xFC00) == 0x0800``. Low bits encode
    ``(unit - 1) * 2 + (1 if on else 0)`` per clsText.
    """
    if not 1 <= unit <= 511:
        raise ValueError(f"unit {unit} out of range 1..511")
    return 0x0800 | (((unit - 1) * 2 + (1 if on else 0)) & 0x03FF)


# Hand-rolled fixed-ID events from clsText.cs:1647-... (PHONE/AC_POWER/etc.).
EVENT_PHONE_DEAD: int = 768
EVENT_PHONE_RINGING: int = 769
EVENT_PHONE_OFF_HOOK: int = 770
EVENT_PHONE_ON_HOOK: int = 771
EVENT_AC_POWER_OFF: int = 772
EVENT_AC_POWER_ON: int = 773


# --------------------------------------------------------------------------
# Clausal chains (Phase 5)
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClausalChain:
    """One multi-record clausal program.

    On firmware ≥3.0.0 each clausal program occupies a contiguous run of
    program slots: one head record (WHEN / AT / EVERY), zero or more
    AND/OR condition records, then one or more THEN action records.

    The engine groups the panel's program table into chains by walking
    forward from each clausal head until the next head / non-clausal /
    empty slot. The fields below carry the typed view of each role:

    * ``head`` — the trigger record (WHEN: event-driven, AT: time-of-day,
      EVERY: recurring interval)
    * ``conditions`` — zero or more AND/OR records guarding the action
    * ``actions`` — one or more THEN records firing when conditions pass
    """

    head: Program
    conditions: tuple[Program, ...]
    actions: tuple[Program, ...]


def build_chains(
    programs: tuple[Program, ...],
) -> tuple[ClausalChain, ...]:
    """Walk a slot-ordered Program tuple, gathering clausal chains.

    Heads (WHEN/AT/EVERY) start a chain; subsequent AND/OR/THEN records
    in adjacent slots join it. A chain ends when:
      * the next slot is another head (start of next chain)
      * the next slot is not a multi-record type (FREE, TIMED, etc.)
      * the next slot is empty
      * we run out of records

    Returns chains in head-slot order. Chains with no THEN records are
    dropped (they have no action to fire).
    """
    by_slot: dict[int, Program] = {
        p.slot: p for p in programs if p.slot is not None and not p.is_empty()
    }
    if not by_slot:
        return ()
    heads = sorted(
        (s for s, p in by_slot.items() if p.prog_type in (
            int(ProgramType.WHEN),
            int(ProgramType.AT),
            int(ProgramType.EVERY),
        )),
    )
    out: list[ClausalChain] = []
    for head_slot in heads:
        head = by_slot[head_slot]
        conditions: list[Program] = []
        actions: list[Program] = []
        slot = head_slot + 1
        while slot in by_slot:
            rec = by_slot[slot]
            ptype = rec.prog_type
            if ptype in (
                int(ProgramType.WHEN),
                int(ProgramType.AT),
                int(ProgramType.EVERY),
            ):
                break  # next chain's head
            if ptype == int(ProgramType.AND) or ptype == int(ProgramType.OR):
                conditions.append(rec)
            elif ptype == int(ProgramType.THEN):
                actions.append(rec)
            else:
                break  # ran into a non-clausal record (TIMED, REMARK, ...)
            slot += 1
        if actions:
            out.append(ClausalChain(
                head=head,
                conditions=tuple(conditions),
                actions=tuple(actions),
            ))
    return tuple(out)


def evaluate_conditions(
    conditions: tuple[Program, ...],
    *,
    is_satisfied,
) -> bool:
    """Evaluate an AND/OR condition list against an external predicate.

    Phase 5 v1 keeps the evaluator deliberately simple: each AND/OR
    record is reduced to a boolean by the caller-supplied
    ``is_satisfied(condition_program)`` predicate, then combined with
    standard short-circuit AND-of-OR-groups semantics:

      * Records form left-to-right groups separated by OR boundaries
        — each OR record *starts a new group*.
      * Within a group, every AND record's predicate must be True
        (logical AND).
      * The overall result is True if *any* group's AND-result is True
        (logical OR across groups).

    An empty conditions tuple is unconditionally True — a "WHEN ...
    THEN ..." chain with no AND/OR guard always runs its actions.

    The detailed semantic decode of each AND/OR record (zone-state
    checks, time-of-day comparisons, structured TEMP > 70-style ops)
    is deferred to a follow-up; for now ``is_satisfied`` is the
    integration point tests / HA code use to feed in evaluated values.
    """
    if not conditions:
        return True
    # Split into groups separated by OR records.
    groups: list[list[Program]] = [[]]
    for c in conditions:
        if c.prog_type == int(ProgramType.OR):
            groups.append([c])
        else:
            groups[-1].append(c)
    # Any group whose ANDs all pass = overall pass.
    for group in groups:
        if all(is_satisfied(c) for c in group):
            return True
    return False


# --------------------------------------------------------------------------
# State-aware AND/OR evaluator
# --------------------------------------------------------------------------


class _UnsupportedCondition(Exception):
    """Raised internally when an AND/OR record encodes a check we don't
    yet evaluate. The caller (StateEvaluator) treats this as False —
    "we can't prove this condition", so the chain stays guarded — and
    logs once so the next semantic-decode pass has a punch list.
    """


class StateEvaluator:
    """Decode AND/OR records against a :class:`MockState` snapshot.

    Each AND/OR :class:`Program` record encodes either:

    * **Traditional** (``and_op == 0``): a compact bit-packed condition
      in the ``cond`` u16 selecting a family (ProgramCond: ZONE / CTRL /
      TIME / SEC / OTHER) with an inline operand. Decoded per
      ``clsText.GetConditionalText`` (clsText.cs:2224-2274).

    * **Structured** (``and_op > 0``): a ``Arg1 OP Arg2`` triple where
      Arg1 and Arg2 are typed references (Zone / Unit / Thermostat /
      TimeDate / Constant / …) plus per-type field selectors. The
      operator is one of :class:`CondOP` (EQ / NE / LT / GT / …).

    Coverage in this initial cut:

    Traditional:
      * ZONE family — zone secure / not-ready against MockZoneState
      * CTRL family — unit on / off against MockUnitState
      * SEC family — area in security-mode against MockAreaState
      * OTHER family: ``NEVER`` (always false), ``LIGHT``/``DARK``
        approximated via the engine's sun events when location is set,
        ``AC_POWER_OFF``/``ON`` (no model — return False),
        ``BATTERY_LOW``/``OK`` (no model — return False)
      * TIME family (time-clock enabled/disabled) — no MockState
        slot for time-clock toggles, returns False

    Structured:
      * Zone.CurrentState / ArmingState (== const)
      * Unit.CurrentState / Level (== / >= / <= const)
      * Thermostat.CurrentTemp / Humidity / setpoints (numeric compare)
      * TimeDate.Month / Day / DayOfWeek / Hour / Minute (numeric compare)

    Anything else returns ``False`` and logs at DEBUG once per
    condition class — keeps a chain *guarded* rather than fired-too-
    eagerly when we can't yet decode the predicate.

    Time-related comparisons need a :class:`Clock`; pass one to honor
    Hour/Minute/DayOfWeek predicates correctly. Without a clock those
    return False.
    """

    def __init__(
        self,
        state,
        *,
        clock: Clock | None = None,
        location: PanelLocation | None = None,
    ) -> None:
        self._state = state
        self._clock = clock
        self._location = location

    def __call__(self, condition: Program) -> bool:
        """Evaluate one AND/OR record against the bound MockState.

        Treated as a plain predicate so callers can pass this instance
        directly to ``ProgramEngine.set_condition_evaluator``.
        """
        try:
            if condition.and_op == CondOP.ARG1_TRADITIONAL:
                return self._eval_traditional(condition)
            return self._eval_structured(condition)
        except _UnsupportedCondition as err:
            _log.debug("evaluator: unsupported condition: %s", err)
            return False
        except Exception:
            _log.exception("evaluator: condition evaluation crashed")
            return False

    # ---- Traditional ------------------------------------------------------

    def _eval_traditional(self, c: Program) -> bool:
        """Decode a Traditional AND record.

        Per ``clsConditionLine.Cond`` (clsConditionLine.cs:17-33), the
        compact-form cond is split across two AND-record slots:

        * disk byte 1 (= ``and_family``) carries the compact's high byte
          (the family + selector encoding from GetConditionalText)
        * disk bytes 3-4 (= ``and_arg1_ix``, ``and_instance`` derived
          from ``cond2 >> 8``) carry the compact's low byte (the
          object index, shifted into the high half)

        Family decoding mirrors clsText.GetConditionalText (clsText.cs:
        2224-2274):

          family & 0xFC == 0x00 → OTHER  (low 4 bits = MiscConditional)
          family & 0xFC == 0x04 → ZONE   (bit 0x02 = NOT_READY, else SECURE)
          family & 0xFC == 0x08 → CTRL   (bit 0x02 = ON,         else OFF)
          family & 0xFC == 0x0C → TIME   (bit 0x02 = ENABLED,    else DIS.)
          family >= 0x10        → SEC    (high nibble = mode, low = area)
        """
        family = c.and_family
        instance = c.and_instance
        family_major = family & 0xFC
        secondary = bool(family & 0x02)  # selector bit within the family
        if family_major == 0x00:
            return self._eval_other(family & 0x0F)
        if family_major == ProgramCond.ZONE:
            return self._eval_traditional_zone(instance, want_not_ready=secondary)
        if family_major == ProgramCond.CTRL:
            return self._eval_traditional_ctrl(instance, want_on=secondary)
        if family_major == ProgramCond.TIME:
            # Time-clock enabled / disabled — MockState doesn't model
            # the enable bit, so we conservatively report disabled.
            return False
        # 0x10 and above: SEC family — high nibble = mode, low nibble = area.
        return self._eval_traditional_sec(
            area=family & 0x0F, mode=(family >> 4) & 0x07,
        )

    def _eval_traditional_zone(self, zone_num: int, *, want_not_ready: bool) -> bool:
        """SECURE matches ``current_state == 0``; NOT_READY matches any
        nonzero current_state (per the panel's display semantics)."""
        zone = self._state.zones.get(zone_num)
        if zone is None:
            # Undefined zone reads as SECURE (matches real-panel behaviour
            # when a programmed zone slot doesn't exist).
            return not want_not_ready
        if want_not_ready:
            return zone.current_state != 0
        return zone.current_state == 0

    def _eval_traditional_ctrl(self, unit_num: int, *, want_on: bool) -> bool:
        """ON matches any nonzero ``state``; OFF matches ``state == 0``.

        MockUnitState.state encodes 0=off, 1=on, 100..200=dim level —
        all nonzero values count as "on" for this predicate, which
        matches the panel's binary on/off display.
        """
        unit = self._state.units.get(unit_num)
        if unit is None:
            return not want_on  # missing unit reads as OFF
        on = unit.state != 0
        return on == want_on

    def _eval_traditional_sec(self, *, area: int, mode: int) -> bool:
        """Area in security-mode N. ``area == 0`` means "any area in
        this mode" per GetConditionalText:2262 — without a multi-area
        model we approximate by checking area 1."""
        if area == 0:
            area = 1
        a = self._state.areas.get(area)
        if a is None:
            return False
        return a.mode == mode

    def _eval_other(self, misc_code: int) -> bool:
        """OTHER family: low 4 bits = enuMiscConditional."""
        try:
            cat = MiscConditional(misc_code)
        except ValueError:
            raise _UnsupportedCondition(f"unknown misc condition {misc_code}")
        if cat == MiscConditional.NONE:
            return True
        if cat == MiscConditional.NEVER:
            return False
        if cat in (MiscConditional.LIGHT, MiscConditional.DARK):
            light = self._is_light_outside()
            if light is None:
                # Can't determine — be conservative, don't fire either way.
                return False
            return light if cat == MiscConditional.LIGHT else not light
        # PHONE_*, AC_POWER_*, BATTERY_*, ENERGY_COST_* — no MockState
        # model for any of these yet. Conservatively False.
        return False

    def _is_light_outside(self) -> bool | None:
        """Approximate "is the sun up" against the engine's PanelLocation
        + clock. Returns None when clock or location is missing
        (caller decides what to do with the indeterminate result)."""
        if self._clock is None or self._location is None:
            return None
        try:
            now = self._clock.now()
            sunrise, sunset = _sun_events(self._location, now.date())
        except Exception:
            return None
        return sunrise <= now <= sunset

    # ---- Structured -------------------------------------------------------

    def _eval_structured(self, c: Program) -> bool:
        """Evaluate ``Arg1 OP Arg2`` style conditions.

        Resolves Arg1 and Arg2 to numeric values via :meth:`_resolve_arg`
        then compares with the operator. Comparison operators are
        straightforward integer math; AND/OR/XOR at this layer are
        treated as bitwise reductions on the resolved values (matches
        the C# operator semantics for those uncommon op codes).
        """
        op = c.and_op
        arg1 = self._resolve_arg(
            c.and_arg1_argtype, c.and_arg1_ix, c.and_arg1_field,
        )
        arg2 = self._resolve_arg(
            c.and_arg2_argtype, c.and_arg2_ix, c.and_arg2_field,
        )
        if arg1 is None or arg2 is None:
            return False
        if op == CondOP.ARG1_EQ_ARG2:
            return arg1 == arg2
        if op == CondOP.ARG1_NE_ARG2:
            return arg1 != arg2
        if op == CondOP.ARG1_LT_ARG2:
            return arg1 < arg2
        if op == CondOP.ARG1_GT_ARG2:
            return arg1 > arg2
        if op == CondOP.ARG1_ODD:
            return (arg1 & 1) == 1
        if op == CondOP.ARG1_EVEN:
            return (arg1 & 1) == 0
        # MULTIPLE / IN / NOT_IN are bitfield checks the panel uses for
        # day-of-week and area-set tests. arg2 is the bitmask.
        if op == CondOP.ARG1_MULTIPLE_ARG2:
            return arg2 != 0 and (arg1 % arg2) == 0
        if op == CondOP.ARG1_IN_ARG2:
            return bool(arg1 & arg2)
        if op == CondOP.ARG1_NOT_IN_ARG2:
            return not bool(arg1 & arg2)
        raise _UnsupportedCondition(f"unknown structured op {op}")

    def _resolve_arg(
        self, argtype: int, ix: int, field: int,
    ) -> int | None:
        """Return the numeric value of one Arg side, or None if it can't
        be resolved (unknown type, missing object, missing clock).
        """
        if argtype == CondArgType.CONSTANT:
            return ix
        if argtype == CondArgType.ZONE:
            return self._resolve_zone_field(ix, field)
        if argtype == CondArgType.UNIT:
            return self._resolve_unit_field(ix, field)
        if argtype == CondArgType.THERMOSTAT:
            return self._resolve_thermostat_field(ix, field)
        if argtype == CondArgType.AREA:
            return self._resolve_area_field(ix, field)
        if argtype == CondArgType.TIME_DATE:
            return self._resolve_timedate_field(field)
        # USER_SETTING, AUXILLARY, AUDIO, ACCESS_CONTROL, MESSAGE,
        # SYSTEM — no MockState models for these. Treat as unresolved
        # so the comparison returns False.
        raise _UnsupportedCondition(f"unsupported argtype {argtype}")

    def _resolve_zone_field(self, ix: int, field: int) -> int | None:
        zone = self._state.zones.get(ix)
        if zone is None:
            return None
        # enuZoneField: LoopReading=1, CurrentState=2, ArmingState=3, AlarmState=4
        if field == 1:
            return zone.loop
        if field == 2:
            return zone.current_state
        if field == 3:
            return zone.arming_state
        if field == 4:
            return zone.latched_state
        raise _UnsupportedCondition(f"zone field {field}")

    def _resolve_unit_field(self, ix: int, field: int) -> int | None:
        unit = self._state.units.get(ix)
        if unit is None:
            return None
        # enuUnitField: CurrentState=1, PreviousState=2, Timer=3, Level=4
        if field == 1:
            # 0 = off, 1 = on, 100..200 = dim. The panel treats anything
            # non-zero as "on" at this granularity.
            return 1 if unit.state != 0 else 0
        if field == 3:
            return unit.time_remaining
        if field == 4:
            # Level: panel returns 0..100% — derive from state byte.
            if unit.state >= 100:
                return unit.state - 100
            return 100 if unit.state == 1 else 0
        raise _UnsupportedCondition(f"unit field {field}")

    def _resolve_thermostat_field(self, ix: int, field: int) -> int | None:
        t = self._state.thermostats.get(ix)
        if t is None:
            return None
        # enuThermostatField map (subset MockState has data for):
        # CurrentTemp=1, HeatSetpt=2, CoolSetpt=3, SystemMode=4, FanMode=5,
        # HoldMode=6, Humidity=9, HumidifySetpoint=10, DehumidifySetpoint=11,
        # OutdoorTemperature=12
        return {
            1: t.temperature_raw,
            2: t.heat_setpoint_raw,
            3: t.cool_setpoint_raw,
            4: t.system_mode,
            5: t.fan_mode,
            6: t.hold_mode,
            9: t.humidity_raw,
            10: t.humidify_setpoint_raw,
            11: t.dehumidify_setpoint_raw,
            12: t.outdoor_temperature_raw,
        }.get(field, None)

    def _resolve_area_field(self, ix: int, field: int) -> int | None:
        area = self._state.areas.get(ix)
        if area is None:
            return None
        # No enuAreaField source — be permissive: field 1 = mode.
        if field == 1:
            return area.mode
        raise _UnsupportedCondition(f"area field {field}")

    def _resolve_timedate_field(self, field: int) -> int | None:
        if self._clock is None:
            return None
        now = self._clock.now()
        # enuTimeDateField: Date=1, Year=2, Month=3, Day=4, DayOfWeek=5,
        # Time=6, DST_Flag=7, Hour=8, Minute=9, SunriseSunset=10.
        if field == 2:
            return now.year
        if field == 3:
            return now.month
        if field == 4:
            return now.day
        if field == 5:
            # DayOfWeek: panel uses 1=Mon..7=Sun per clsHAC. Python
            # weekday() returns Mon=0..Sun=6 — add 1.
            return now.weekday() + 1
        if field == 6:
            # Time-of-day encoded as (hour * 60 + minute) — minutes since
            # midnight. Matches the C# packing in GetComplexConditionText.
            return now.hour * 60 + now.minute
        if field == 8:
            return now.hour
        if field == 9:
            return now.minute
        # Date / DST_Flag / SunriseSunset — not modelled here.
        raise _UnsupportedCondition(f"timedate field {field}")


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


@dataclass
class _EngineMetrics:
    """Lightweight counters useful in tests + diagnostics."""

    timed_fired: int = 0
    event_fired: int = 0
    yearly_fired: int = 0
    clausal_fired: int = 0
    errors: int = 0


class ProgramEngine:
    """Run a panel's programs autonomously against a :class:`MockPanel`.

    Phase 1 (this skeleton) classifies the programs and stands up the
    asyncio task harness but doesn't fire anything yet. Subsequent
    phases plug in TIMED / YEARLY / EVENT / clausal execution.

    Lifecycle::

        engine = ProgramEngine(panel, clock=FakeClock(t0))
        await engine.start()        # spawns the per-bucket tasks
        ...                          # tests advance the clock / emit events
        await engine.stop()         # cancels and awaits all tasks

    The engine is safe to instantiate without ever calling ``start`` —
    the classification work happens up front but no tasks spawn until
    explicit start.
    """

    def __init__(
        self,
        panel: "MockPanel",
        *,
        clock: Clock | None = None,
        location: PanelLocation | None = None,
    ) -> None:
        self._panel = panel
        self._clock = clock or RealClock()
        self._location = location
        # Decode raw bytes from MockState.programs into Program objects
        # once, at construction. Reclassifying on every start would be
        # wasteful and would also lose the slot indices.
        decoded: list[Program] = []
        for slot, raw in panel.state.programs.items():
            try:
                decoded.append(Program.from_wire_bytes(raw, slot=slot))
            except Exception:
                # Malformed records are skipped, not fatal. The engine
                # carries on with whatever is decodable.
                continue
        self._programs: tuple[Program, ...] = tuple(decoded)
        self._classified = classify(self._programs)
        self._chains = build_chains(self._programs)
        self._tasks: list[asyncio.Task[None]] = []
        self._running = False
        # event_id → list of EVENT programs *and* WHEN-headed clausal
        # chains subscribed to it. Built lazily in start().
        self._event_table: dict[int, list[Program]] = {}
        self._when_chain_table: dict[int, list[ClausalChain]] = {}
        # External hook (defaults to "all conditions pass") for evaluating
        # AND/OR records. Tests / HA replace this to model real state.
        self._condition_evaluator = self._default_condition_evaluator
        self.metrics = _EngineMetrics()

    @property
    def chains(self) -> tuple[ClausalChain, ...]:
        """All clausal chains decoded from the panel's program table."""
        return self._chains

    def set_condition_evaluator(self, fn) -> None:
        """Replace the AND/OR condition evaluator.

        ``fn`` is called with each AND/OR program record and must return
        bool. The default returns True for every AND, False for every
        OR (a degenerate evaluator that means "all chains' first AND
        groups always pass" — useful as a smoke-test default, not for
        real automation).

        For real automation, call :meth:`use_state_evaluator` instead
        (or build your own :class:`StateEvaluator` and pass it here).
        """
        self._condition_evaluator = fn

    def use_state_evaluator(self) -> "StateEvaluator":
        """Install a :class:`StateEvaluator` bound to this engine's
        ``MockState``, clock, and location. Returns the new evaluator
        so the caller can introspect it.

        Equivalent to ``engine.set_condition_evaluator(
        StateEvaluator(panel.state, clock=clock, location=loc))``.
        """
        evaluator = StateEvaluator(
            self._panel.state, clock=self._clock, location=self._location,
        )
        self._condition_evaluator = evaluator
        return evaluator

    @staticmethod
    def _default_condition_evaluator(condition: Program) -> bool:
        """Stub evaluator — caller should override via set_condition_evaluator."""
        return condition.prog_type == int(ProgramType.AND)

    # ---- inspection -------------------------------------------------------

    @property
    def clock(self) -> Clock:
        """The clock this engine is driven by."""
        return self._clock

    @property
    def classified(self) -> _ClassifiedPrograms:
        """Programs split into execution buckets. Useful in tests to
        confirm the engine sees what you expect."""
        return self._classified

    @property
    def running(self) -> bool:
        return self._running

    # ---- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Begin executing programs in the background.

        Idempotent — calling start on a running engine is a no-op.
        """
        if self._running:
            return
        self._running = True
        # Phase 2: one worker task per TIMED program.
        for program in self._classified.timed:
            self._tasks.append(
                asyncio.create_task(
                    self._run_timed_program(program),
                    name=f"omni-pca-timed-slot-{program.slot}",
                )
            )
        # Phase 3: one worker per YEARLY program.
        for program in self._classified.yearly:
            self._tasks.append(
                asyncio.create_task(
                    self._run_yearly_program(program),
                    name=f"omni-pca-yearly-slot-{program.slot}",
                )
            )
        # Phase 4: EVENT programs aren't long-running tasks — they just
        # register in the event table and the engine dispatches on
        # emit_event(). Build the table now so emit is O(1).
        self._event_table.clear()
        for program in self._classified.event:
            self._event_table.setdefault(program.event_id, []).append(program)
        # Phase 5: clausal chains. AT and EVERY chains spawn worker
        # tasks; WHEN chains register in a parallel event-dispatch table
        # so emit_event() fires both raw EVENT programs and matching
        # WHEN chains.
        self._when_chain_table.clear()
        for chain in self._chains:
            if chain.head.prog_type == int(ProgramType.WHEN):
                self._when_chain_table.setdefault(
                    chain.head.event_id, []
                ).append(chain)
            elif chain.head.prog_type == int(ProgramType.AT):
                self._tasks.append(
                    asyncio.create_task(
                        self._run_at_chain(chain),
                        name=f"omni-pca-at-chain-{chain.head.slot}",
                    )
                )
            elif chain.head.prog_type == int(ProgramType.EVERY):
                self._tasks.append(
                    asyncio.create_task(
                        self._run_every_chain(chain),
                        name=f"omni-pca-every-chain-{chain.head.slot}",
                    )
                )

    async def _run_timed_program(self, program: Program) -> None:
        """Sleep-until-next-fire loop for one TIMED program.

        Handles both ABSOLUTE (wall-clock hour:minute) and sunrise /
        sunset-relative time kinds. Sun-relative programs only run if
        the engine was given a :class:`PanelLocation`; without one they
        return immediately, the same way an empty Days mask would.
        """
        try:
            while self._running:
                now = self._clock.now()
                if program.time_kind == TimeKind.ABSOLUTE:
                    next_fire = _next_absolute_fire(now, program)
                elif self._location is None:
                    _log.debug(
                        "engine: TIMED slot %s is sun-relative but no "
                        "location was supplied — skipping",
                        program.slot,
                    )
                    return
                else:
                    next_fire = _next_sun_relative_fire(now, program, self._location)
                if next_fire is None:
                    return  # disabled (empty Days, sun unavailable, etc.)
                await self._clock.sleep_until(next_fire)
                if not self._running:
                    return
                await self._fire(program)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "engine: TIMED slot %s crashed", program.slot,
            )
            self.metrics.errors += 1

    # ---- event dispatch (Phase 4) ----------------------------------------

    async def emit_event(self, event_id: int) -> int:
        """Fire every EVENT program subscribed to ``event_id``.

        Returns the number of programs that fired. Safe to call before
        ``start()`` (returns 0 since no event table is built yet) or
        after ``stop()`` (programs registered while running aren't
        retained — call start again to rebuild).

        The classic use cases are wired up via the convenience helpers
        below, but tests and HA code can also call this directly with
        any raw ``event_id``.
        """
        if not self._running:
            return 0
        programs = self._event_table.get(event_id, ())
        for program in programs:
            await self._fire(program)
        fired = len(programs)
        # Plus any WHEN-headed clausal chains subscribed to this event.
        for chain in self._when_chain_table.get(event_id, ()):
            if await self._fire_chain(chain):
                fired += 1
        return fired

    async def emit_user_macro_button(self, button: int) -> int:
        """Convenience: fire EVENT programs subscribed to a button press."""
        return await self.emit_event(event_id_user_macro_button(button))

    async def emit_zone_state(self, zone: int, state: int) -> int:
        """Convenience: fire EVENT programs subscribed to a zone-state change.

        ``state`` is the 2-bit current_state code: 0=secure, 1=not-ready,
        2=trouble, 3=tamper. Matches MockZoneState.current_state.
        """
        return await self.emit_event(event_id_zone_state(zone, state))

    async def emit_unit_state(self, unit: int, on: bool) -> int:
        """Convenience: fire EVENT programs subscribed to a unit on/off."""
        return await self.emit_event(event_id_unit_state(unit, on))

    async def _run_yearly_program(self, program: Program) -> None:
        """Sleep-until-next-fire loop for one YEARLY program."""
        try:
            while self._running:
                next_fire = _next_yearly_fire(self._clock.now(), program)
                if next_fire is None:
                    return  # disabled or invalid month/day
                await self._clock.sleep_until(next_fire)
                if not self._running:
                    return
                await self._fire(program)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception(
                "engine: YEARLY slot %s crashed", program.slot,
            )
            self.metrics.errors += 1

    async def _fire_chain(self, chain: ClausalChain) -> bool:
        """Evaluate a chain's AND/OR conditions; if they pass, fire every
        THEN action. Returns True iff the conditions passed.

        Each fired THEN action goes through the same wire-handler path
        as TIMED/YEARLY/EVENT programs.
        """
        try:
            passed = evaluate_conditions(
                chain.conditions, is_satisfied=self._condition_evaluator,
            )
        except Exception:
            _log.exception(
                "engine: chain %s condition evaluation raised",
                chain.head.slot,
            )
            self.metrics.errors += 1
            return False
        if not passed:
            return False
        for action in chain.actions:
            await self._fire(action)
        return True

    async def _run_at_chain(self, chain: ClausalChain) -> None:
        """Sleep-until-next-fire loop for an AT-headed chain.

        AT records carry the same TIMED fields (hour/minute/days/
        time_kind/time_offset) as compact-form TIMED programs, so we
        reuse the same scheduling primitives.
        """
        try:
            while self._running:
                now = self._clock.now()
                head = chain.head
                if head.time_kind == TimeKind.ABSOLUTE:
                    next_fire = _next_absolute_fire(now, head)
                elif self._location is None:
                    return
                else:
                    next_fire = _next_sun_relative_fire(now, head, self._location)
                if next_fire is None:
                    return
                await self._clock.sleep_until(next_fire)
                if not self._running:
                    return
                await self._fire_chain(chain)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("engine: AT chain slot %s crashed", chain.head.slot)
            self.metrics.errors += 1

    async def _run_every_chain(self, chain: ClausalChain) -> None:
        """Sleep-until-next-fire loop for an EVERY-headed chain.

        Interval is in seconds per :meth:`Program.every_interval`. Zero
        disables the chain (matches real-panel behaviour for an
        unconfigured EVERY record).
        """
        interval_sec = chain.head.every_interval
        if interval_sec <= 0:
            return
        delay = timedelta(seconds=interval_sec)
        try:
            while self._running:
                await self._clock.sleep_until(self._clock.now() + delay)
                if not self._running:
                    return
                await self._fire_chain(chain)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("engine: EVERY chain slot %s crashed", chain.head.slot)
            self.metrics.errors += 1

    async def _fire(self, program: Program) -> None:
        """Execute one program by feeding its command through the same
        wire-handler path the v2 Command opcode uses."""
        try:
            self._panel._handle_command(_command_payload(program))
        except Exception:
            _log.exception(
                "engine: firing slot %s (cmd=%d par=%d pr2=%d) raised",
                program.slot, program.cmd, program.par, program.pr2,
            )
            self.metrics.errors += 1
            return
        kind = ProgramType(program.prog_type)
        if kind == ProgramType.TIMED:
            self.metrics.timed_fired += 1
        elif kind == ProgramType.EVENT:
            self.metrics.event_fired += 1
        elif kind == ProgramType.YEARLY:
            self.metrics.yearly_fired += 1
        else:
            self.metrics.clausal_fired += 1

    async def stop(self) -> None:
        """Cancel all engine-spawned tasks and wait for them to exit.

        Idempotent."""
        if not self._running:
            return
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

    async def __aenter__(self) -> "ProgramEngine":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()
