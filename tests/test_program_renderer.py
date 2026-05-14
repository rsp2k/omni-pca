"""Tests for the structured-English program renderer.

Coverage strategy:
* Each trigger / condition / action branch gets at least one focused
  test asserting the rendered tokens (or plain-text projection).
* End-to-end tests build a Program (or ClausalChain) that mirrors what
  PC Access produces and verify the renderer's output reads cleanly.
* Live-state overlay is tested separately via a small fake StateResolver
  so we can assert the badges land on the right REF tokens.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from omni_pca.commands import Command
from omni_pca.mock_panel import (
    MockAreaState,
    MockState,
    MockThermostatState,
    MockUnitState,
    MockZoneState,
)
from omni_pca.program_engine import (
    ClausalChain,
    EVENT_AC_POWER_OFF,
    event_id_unit_state,
    event_id_user_macro_button,
    event_id_zone_state,
)
from omni_pca.program_renderer import (
    AccountNameResolver,
    MockStateResolver,
    NameResolver,
    ProgramRenderer,
    StateResolver,
    Token,
    TokenKind,
    _format_interval,
    format_days,
    tokens_to_string,
)
from omni_pca.programs import (
    CondArgType,
    CondOP,
    Days,
    Program,
    ProgramType,
)


# ---- Test helpers --------------------------------------------------------


class _StaticNameResolver:
    """Trivial name resolver — explicit name dict, useful in unit tests."""

    def __init__(self, names: dict[tuple[str, int], str]) -> None:
        self._names = names

    def name_of(self, kind: str, index: int) -> str | None:
        return self._names.get((kind, index))


class _StaticStateResolver:
    """Trivial state resolver — explicit state dict."""

    def __init__(self, states: dict[tuple[str, int], str]) -> None:
        self._states = states

    def state_of(self, kind: str, index: int) -> str | None:
        return self._states.get((kind, index))


def _renderer_with(
    names: dict[tuple[str, int], str] | None = None,
    states: dict[tuple[str, int], str] | None = None,
) -> ProgramRenderer:
    return ProgramRenderer(
        names=_StaticNameResolver(names or {}),
        state=_StaticStateResolver(states) if states is not None else None,
    )


# ---- Format helpers ------------------------------------------------------


def test_format_days_everyday() -> None:
    assert format_days(int(
        Days.MONDAY | Days.TUESDAY | Days.WEDNESDAY | Days.THURSDAY
        | Days.FRIDAY | Days.SATURDAY | Days.SUNDAY
    )) == "every day"


def test_format_days_weekdays() -> None:
    assert format_days(int(
        Days.MONDAY | Days.TUESDAY | Days.WEDNESDAY | Days.THURSDAY | Days.FRIDAY
    )) == "weekdays"


def test_format_days_weekend() -> None:
    assert format_days(int(Days.SATURDAY | Days.SUNDAY)) == "weekends"


def test_format_days_individual_days() -> None:
    assert format_days(int(Days.MONDAY | Days.WEDNESDAY | Days.FRIDAY)) == "Mon, Wed, Fri"


def test_format_days_zero() -> None:
    assert format_days(0) == "never"


def test_format_interval_seconds() -> None:
    assert _format_interval(5) == "5 sec"
    assert _format_interval(45) == "45 sec"


def test_format_interval_minutes_and_hours() -> None:
    assert _format_interval(300) == "5 min"
    assert _format_interval(7200) == "2 hr"


def test_format_interval_disabled() -> None:
    assert _format_interval(0) == "(disabled)"


# ---- Tokens to string ----------------------------------------------------


def test_tokens_to_string_with_state_badge() -> None:
    """REF tokens with `state` set surface as ``name [state]``."""
    tokens = [
        Token(TokenKind.KEYWORD, "WHEN"),
        Token(TokenKind.TEXT, " "),
        Token(TokenKind.REF, "Front Door",
              entity_kind="zone", entity_id=1, state="SECURE"),
        Token(TokenKind.TEXT, " is opened"),
    ]
    assert tokens_to_string(tokens) == "WHEN Front Door [SECURE] is opened"


def test_tokens_to_string_handles_newline_and_indent() -> None:
    tokens = [
        Token(TokenKind.KEYWORD, "WHEN"),
        Token(TokenKind.TEXT, " trigger"),
        Token(TokenKind.NEWLINE, ""),
        Token(TokenKind.INDENT, "  "),
        Token(TokenKind.KEYWORD, "AND IF"),
        Token(TokenKind.TEXT, " condition"),
    ]
    assert tokens_to_string(tokens) == "WHEN trigger\n  AND IF condition"


# ---- Trigger rendering ---------------------------------------------------


def test_render_timed_program() -> None:
    """AT 06:00 weekdays → Turn ON LIVING_LAMP."""
    p = Program(
        slot=42, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0,
        days=int(Days.MONDAY | Days.TUESDAY | Days.WEDNESDAY
                 | Days.THURSDAY | Days.FRIDAY),
    )
    r = _renderer_with(names={("unit", 7): "LIVING_LAMP"})
    text = tokens_to_string(r.render_program(p))
    assert text == "AT 06:00 weekdays\nTHEN Turn ON LIVING_LAMP"


def test_render_event_program_zone_state_change() -> None:
    """EVENT triggered by zone 5 not-ready → unit 3 OFF."""
    evt = event_id_zone_state(5, 1)  # zone 5 becomes not-ready
    p = Program(
        slot=10, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_OFF), pr2=3,
        month=(evt >> 8) & 0xFF, day=evt & 0xFF,
    )
    r = _renderer_with(names={
        ("zone", 5): "FRONT_DOOR",
        ("unit", 3): "PORCH_LIGHT",
    })
    assert tokens_to_string(r.render_program(p)) == (
        "WHEN FRONT_DOOR becomes not ready\n"
        "THEN Turn OFF PORCH_LIGHT"
    )


def test_render_yearly_program() -> None:
    p = Program(
        slot=99, prog_type=int(ProgramType.YEARLY),
        cmd=int(Command.UNIT_ON), pr2=12,
        month=12, day=25, hour=18, minute=30,
    )
    r = _renderer_with(names={("unit", 12): "CHRISTMAS_LIGHTS"})
    assert tokens_to_string(r.render_program(p)) == (
        "ON 12/25 at 18:30\n"
        "THEN Turn ON CHRISTMAS_LIGHTS"
    )


def test_render_remark_program() -> None:
    """Remark records render as 'REMARK #N'."""
    p = Program(
        slot=5, prog_type=int(ProgramType.REMARK),
        remark_id=42,
    )
    r = _renderer_with()
    assert tokens_to_string(r.render_program(p)) == "REMARK #42"


def test_render_free_slot() -> None:
    p = Program(slot=1, prog_type=int(ProgramType.FREE))
    r = _renderer_with()
    assert tokens_to_string(r.render_program(p)) == "(empty slot)"


# ---- Event-ID decoding ---------------------------------------------------


def test_render_event_button_press() -> None:
    """Button-press events render via the button name."""
    evt = event_id_user_macro_button(7)
    p = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=1,
        month=(evt >> 8) & 0xFF, day=evt & 0xFF,
    )
    r = _renderer_with(names={
        ("button", 7): "GOOD_NIGHT",
        ("unit", 1): "BEDROOM_LAMP",
    })
    assert tokens_to_string(r.render_program(p)).startswith(
        "WHEN GOOD_NIGHT is pressed\n"
    )


def test_render_event_unit_state_change() -> None:
    evt = event_id_unit_state(4, on=True)
    p = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_OFF), pr2=5,
        month=(evt >> 8) & 0xFF, day=evt & 0xFF,
    )
    r = _renderer_with(names={("unit", 4): "ALARM", ("unit", 5): "SIREN"})
    assert tokens_to_string(r.render_program(p)) == (
        "WHEN ALARM turns ON\n"
        "THEN Turn OFF SIREN"
    )


def test_render_event_ac_power_lost() -> None:
    p = Program(
        slot=1, prog_type=int(ProgramType.EVENT),
        cmd=int(Command.UNIT_ON), pr2=1,
        month=(EVENT_AC_POWER_OFF >> 8) & 0xFF,
        day=EVENT_AC_POWER_OFF & 0xFF,
    )
    r = _renderer_with(names={("unit", 1): "EMERGENCY_LIGHT"})
    assert tokens_to_string(r.render_program(p)) == (
        "WHEN AC power lost\n"
        "THEN Turn ON EMERGENCY_LIGHT"
    )


# ---- Inline AND conditions (compact form) -------------------------------


def test_render_timed_with_inline_zone_condition() -> None:
    """TIMED program with an inline AND IF ZONE ... SECURE condition."""
    # cond = high byte: 0x04 (ZONE family), low byte: zone 5
    cond = (0x04 << 8) | 5
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=22, minute=30,
        days=int(Days.MONDAY),
        cond=cond,
    )
    r = _renderer_with(names={
        ("zone", 5): "FRONT_DOOR", ("unit", 7): "PORCH_LIGHT",
    })
    assert tokens_to_string(r.render_program(p)) == (
        "AT 22:30 Mon\n"
        "  AND IF FRONT_DOOR is secure\n"
        "THEN Turn ON PORCH_LIGHT"
    )


def test_render_timed_with_inline_unit_on_condition() -> None:
    """TIMED + AND IF UNIT ... ON. Compact cond high byte 0x0A = CTRL+ON."""
    cond = (0x0A << 8) | 3  # CTRL family + ON, unit 3
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0,
        days=int(Days.MONDAY),
        cond=cond,
    )
    r = _renderer_with(names={
        ("unit", 3): "OCCUPANCY", ("unit", 7): "KITCHEN_LIGHT",
    })
    assert tokens_to_string(r.render_program(p)) == (
        "AT 06:00 Mon\n"
        "  AND IF OCCUPANCY is ON\n"
        "THEN Turn ON KITCHEN_LIGHT"
    )


# ---- Clausal chain rendering --------------------------------------------


def _and_traditional(slot: int, family: int, instance: int = 0) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.AND),
        cond=family & 0xFF, cond2=(instance & 0xFF) << 8,
    )


def _or_record(slot: int) -> Program:
    """An empty OR-separator record. PC Access in practice always
    bundles a condition into the OR record itself; use ``_or_traditional``
    for that case. This helper exists for the rare empty-OR cases."""
    return Program(slot=slot, prog_type=int(ProgramType.OR))


def _or_traditional(slot: int, family: int, instance: int = 0) -> Program:
    """OR-alternative record carrying a Traditional condition inline."""
    return Program(
        slot=slot, prog_type=int(ProgramType.OR),
        cond=family & 0xFF, cond2=(instance & 0xFF) << 8,
    )


def _then_record(slot: int, cmd: int, pr2: int, par: int = 0) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.THEN),
        cmd=cmd, pr2=pr2, par=par,
    )


def test_render_when_chain_simple() -> None:
    """WHEN button N pressed → 1 cond → 1 action."""
    evt = event_id_user_macro_button(5)
    when = Program(
        slot=1, prog_type=int(ProgramType.WHEN),
        month=(evt >> 8) & 0xFF, day=evt & 0xFF,
    )
    and_cond = _and_traditional(2, family=0x04, instance=7)  # ZONE 7 secure
    then = _then_record(3, int(Command.UNIT_ON), 9)
    chain = ClausalChain(head=when, conditions=(and_cond,), actions=(then,))
    r = _renderer_with(names={
        ("button", 5): "GOODNIGHT",
        ("zone", 7): "BACK_DOOR",
        ("unit", 9): "HALLWAY",
    })
    assert tokens_to_string(r.render_chain(chain)) == (
        "WHEN GOODNIGHT is pressed\n"
        "  AND IF BACK_DOOR is secure\n"
        "THEN Turn ON HALLWAY"
    )


def test_render_when_chain_with_or_branch_and_multiple_actions() -> None:
    """Full clausal program with OR branch and two THEN actions."""
    evt = event_id_user_macro_button(5)
    when = Program(
        slot=1, prog_type=int(ProgramType.WHEN),
        month=(evt >> 8) & 0xFF, day=evt & 0xFF,
    )
    chain = ClausalChain(
        head=when,
        conditions=(
            _and_traditional(2, family=0x04, instance=7),     # ZONE 7 secure
            _or_traditional(3, family=0x0A, instance=3),      # OR IF UNIT 3 ON
        ),
        actions=(
            _then_record(4, int(Command.UNIT_ON), 9),         # Turn ON HALLWAY
            _then_record(5, int(Command.UNIT_OFF), 10),       # Turn OFF FOYER
        ),
    )
    r = _renderer_with(names={
        ("button", 5): "GOODNIGHT",
        ("zone", 7): "BACK_DOOR",
        ("unit", 3): "MOTION",
        ("unit", 9): "HALLWAY",
        ("unit", 10): "FOYER",
    })
    assert tokens_to_string(r.render_chain(chain)) == (
        "WHEN GOODNIGHT is pressed\n"
        "  AND IF BACK_DOOR is secure\n"
        "  OR IF MOTION is ON\n"
        "THEN Turn ON HALLWAY\n"
        "AND Turn OFF FOYER"
    )


def test_render_at_chain() -> None:
    """AT-headed clausal chain with structured-English output."""
    head = Program(
        slot=1, prog_type=int(ProgramType.AT),
        hour=7, minute=15, days=int(Days.SATURDAY | Days.SUNDAY),
    )
    chain = ClausalChain(
        head=head, conditions=(),
        actions=(_then_record(2, int(Command.UNIT_ON), 12),),
    )
    r = _renderer_with(names={("unit", 12): "COFFEE_MAKER"})
    assert tokens_to_string(r.render_chain(chain)) == (
        "AT 07:15 weekends\n"
        "THEN Turn ON COFFEE_MAKER"
    )


def test_render_every_chain() -> None:
    head = Program(
        slot=1, prog_type=int(ProgramType.EVERY),
        # every_interval = ((cond & 0xFF) << 8) | ((cond2 >> 8) & 0xFF).
        # For interval=60: cond=0, cond2=60<<8=0x3C00 → 60 sec = 1 min.
        cond=0, cond2=60 << 8,
    )
    chain = ClausalChain(
        head=head, conditions=(),
        actions=(_then_record(2, int(Command.UNIT_ON), 1),),
    )
    r = _renderer_with(names={("unit", 1): "AERATOR"})
    assert tokens_to_string(r.render_chain(chain)) == (
        "EVERY 1 min\n"
        "THEN Turn ON AERATOR"
    )


# ---- Structured AND/OR rendering ----------------------------------------


def _and_structured(
    slot: int, op: int,
    arg1_type: int, arg1_ix: int, arg1_field: int,
    arg2_type: int, arg2_ix: int, arg2_field: int = 0,
) -> Program:
    return Program(
        slot=slot, prog_type=int(ProgramType.AND),
        cond=(op << 8) | arg1_type,
        cond2=arg1_ix,
        cmd=arg1_field,
        par=arg2_type,
        pr2=arg2_ix,
        month=arg2_field,
    )


def test_render_structured_zone_current_state_eq_constant() -> None:
    """AND IF Zone(5).CurrentState == 1"""
    and_rec = _and_structured(
        slot=1, op=int(CondOP.ARG1_EQ_ARG2),
        arg1_type=int(CondArgType.ZONE), arg1_ix=5, arg1_field=2,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=1,
    )
    chain = ClausalChain(
        head=Program(slot=0, prog_type=int(ProgramType.WHEN),
                     month=0, day=1),
        conditions=(and_rec,),
        actions=(_then_record(2, int(Command.UNIT_ON), 9),),
    )
    r = _renderer_with(names={
        ("zone", 5): "FRONT_DOOR",
        ("button", 1): "BTN_1",
        ("unit", 9): "HALLWAY",
    })
    text = tokens_to_string(r.render_chain(chain))
    assert "AND IF FRONT_DOOR.CurrentState == 1" in text


def test_render_structured_thermostat_temp_gt_constant() -> None:
    """AND IF Thermostat(1).Temperature > 75"""
    and_rec = _and_structured(
        slot=1, op=int(CondOP.ARG1_GT_ARG2),
        arg1_type=int(CondArgType.THERMOSTAT), arg1_ix=1, arg1_field=1,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=75,
    )
    chain = ClausalChain(
        head=Program(slot=0, prog_type=int(ProgramType.WHEN), month=0, day=1),
        conditions=(and_rec,),
        actions=(_then_record(2, int(Command.UNIT_ON), 1),),
    )
    r = _renderer_with(names={
        ("thermostat", 1): "DOWNSTAIRS",
        ("button", 1): "BTN_1",
        ("unit", 1): "AC",
    })
    text = tokens_to_string(r.render_chain(chain))
    assert "AND IF DOWNSTAIRS.Temperature > 75" in text


def test_render_structured_timedate_hour_eq() -> None:
    """AND IF TimeDate.Hour == 22"""
    and_rec = _and_structured(
        slot=1, op=int(CondOP.ARG1_EQ_ARG2),
        arg1_type=int(CondArgType.TIME_DATE), arg1_ix=0, arg1_field=8,
        arg2_type=int(CondArgType.CONSTANT), arg2_ix=22,
    )
    chain = ClausalChain(
        head=Program(slot=0, prog_type=int(ProgramType.WHEN), month=0, day=1),
        conditions=(and_rec,),
        actions=(_then_record(2, int(Command.UNIT_ON), 1),),
    )
    r = _renderer_with(names={("button", 1): "BTN", ("unit", 1): "LIGHT"})
    text = tokens_to_string(r.render_chain(chain))
    assert "AND IF Hour == 22" in text


# ---- Action verb rendering ----------------------------------------------


def test_render_action_bypass_zone() -> None:
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.BYPASS_ZONE), pr2=5,
        hour=22, minute=0, days=int(Days.MONDAY),
    )
    r = _renderer_with(names={("zone", 5): "WINDOW"})
    assert "THEN Bypass WINDOW" in tokens_to_string(r.render_program(p))


def test_render_action_unit_level_with_percentage() -> None:
    """UNIT_LEVEL uses ``par`` as the percentage."""
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_LEVEL), pr2=7, par=50,
        hour=6, minute=0, days=int(Days.MONDAY),
    )
    r = _renderer_with(names={("unit", 7): "DIMMER"})
    assert "THEN Set level DIMMER to 50%" in tokens_to_string(r.render_program(p))


def test_render_action_security_arm() -> None:
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.SECURITY_AWAY), pr2=1,
        hour=22, minute=0, days=int(Days.MONDAY),
    )
    r = _renderer_with(names={("area", 1): "MAIN"})
    assert "THEN Arm Away MAIN" in tokens_to_string(r.render_program(p))


# ---- Live-state overlay --------------------------------------------------


def test_live_state_overlay_appears_in_string() -> None:
    """When a state resolver is set, REF tokens get bracketed badges."""
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0, days=int(Days.MONDAY),
    )
    r = _renderer_with(
        names={("unit", 7): "LIVING_LAMP"},
        states={("unit", 7): "OFF"},
    )
    text = tokens_to_string(r.render_program(p))
    assert "Turn ON LIVING_LAMP [OFF]" in text


def test_live_state_overlay_tokens_carry_state_field() -> None:
    """REF tokens themselves have .state populated — not just the text."""
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=6, minute=0, days=int(Days.MONDAY),
    )
    r = _renderer_with(
        names={("unit", 7): "LIVING_LAMP"},
        states={("unit", 7): "ON 50%"},
    )
    refs = [t for t in r.render_program(p) if t.kind == TokenKind.REF]
    assert len(refs) == 1
    assert refs[0].entity_kind == "unit"
    assert refs[0].entity_id == 7
    assert refs[0].state == "ON 50%"


def test_live_state_absent_when_resolver_returns_none() -> None:
    """A resolver that doesn't know about an entity omits the badge."""
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=99,
        hour=6, minute=0, days=int(Days.MONDAY),
    )
    r = _renderer_with(states={("unit", 7): "ON"})  # nothing for unit 99
    text = tokens_to_string(r.render_program(p))
    assert "[" not in text  # no badge anywhere


# ---- MockStateResolver end-to-end ---------------------------------------


def test_mock_state_resolver_zone_badge() -> None:
    state = MockState(zones={
        5: MockZoneState(name="FRONT_DOOR", current_state=1),  # not-ready
    })
    res = MockStateResolver(state)
    assert res.name_of("zone", 5) == "FRONT_DOOR"
    assert res.state_of("zone", 5) == "NOT READY"


def test_mock_state_resolver_unit_on_with_dim_level() -> None:
    state = MockState(units={3: MockUnitState(name="DIMMER", state=150)})
    res = MockStateResolver(state)
    assert res.state_of("unit", 3) == "ON 50%"


def test_mock_state_resolver_area_security_mode() -> None:
    state = MockState(areas={1: MockAreaState(name="MAIN", mode=3)})
    res = MockStateResolver(state)
    assert res.state_of("area", 1) == "Away"


def test_mock_state_resolver_thermostat_temperature() -> None:
    state = MockState(thermostats={1: MockThermostatState(temperature_raw=170)})
    res = MockStateResolver(state)
    # raw 170 / 2 - 40 = 45°F (low side of the linear scale)
    assert res.state_of("thermostat", 1) == "45°F"


def test_mock_state_resolver_unknown_kind_returns_none() -> None:
    res = MockStateResolver(MockState())
    assert res.state_of("nonexistent", 1) is None


# ---- AccountNameResolver end-to-end -------------------------------------


def test_account_name_resolver_pulls_from_account() -> None:
    @dataclass
    class _AcctStub:
        zone_names: dict[int, str]
        unit_names: dict[int, str]

    acct = _AcctStub(
        zone_names={1: "FRONT", 2: "BACK"},
        unit_names={5: "LAMP"},
    )
    res = AccountNameResolver(acct)
    assert res.name_of("zone", 1) == "FRONT"
    assert res.name_of("unit", 5) == "LAMP"
    assert res.name_of("zone", 99) is None
    assert res.name_of("area", 1) is None  # no area_names on stub


# ---- Summary (one-liner) --------------------------------------------------


def test_summarize_timed_program() -> None:
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=22, minute=30, days=int(Days.MONDAY),
    )
    r = _renderer_with(names={("unit", 7): "LAMP"})
    assert tokens_to_string(r.summarize_program(p)) == (
        "22:30 Mon → Turn ON LAMP"
    )


def test_summarize_compact_program_with_conditions() -> None:
    """Summary shows count of inline conditions."""
    cond = (0x04 << 8) | 5  # AND IF zone 5 secure
    p = Program(
        slot=1, prog_type=int(ProgramType.TIMED),
        cmd=int(Command.UNIT_ON), pr2=7,
        hour=22, minute=30, days=int(Days.MONDAY),
        cond=cond,
    )
    r = _renderer_with(names={("unit", 7): "LAMP", ("zone", 5): "DOOR"})
    text = tokens_to_string(r.summarize_program(p))
    assert "(+1 cond)" in text


# ---- Live-fixture smoke test --------------------------------------------


def test_renderer_handles_every_program_in_live_fixture() -> None:
    """Every defined program in the live .pca fixture renders cleanly.

    This is the broadest correctness signal: 330 real homeowner-authored
    programs with names, conditions, and actions, all decoded by the
    same code path the HA panel will use. Skipped when the gitignored
    fixture isn't on disk.
    """
    from pathlib import Path

    fixture = Path("/home/kdm/home-auto/HAI/pca-re/extracted/Our_House.pca.plain")
    if not fixture.is_file():
        pytest.skip(f"fixture not available: {fixture}")
    from omni_pca.pca_file import KEY_EXPORT, decrypt_pca_bytes, parse_pca_file

    acct = parse_pca_file(decrypt_pca_bytes(fixture.read_bytes(), KEY_EXPORT),
                          key=KEY_EXPORT)
    r = ProgramRenderer(names=AccountNameResolver(acct))
    defined = [p for p in acct.programs if not p.is_empty()]
    assert len(defined) == 330

    # Every program produces a non-empty summary + full render. No
    # exception should escape — the renderer's job is to be informative
    # even for records it doesn't fully understand.
    for p in defined:
        summary = tokens_to_string(r.summarize_program(p))
        full = tokens_to_string(r.render_program(p))
        assert summary
        assert full

    # The first few programs in this fixture are button-press chains
    # against the garage doors — confirm the rendering reads the way
    # we expect ("WHEN ... is pressed AND IF ... is secure THEN ...").
    slot1 = tokens_to_string(r.render_program(acct.programs[0]))
    assert slot1.startswith("WHEN ")
    assert "is pressed" in slot1
    assert "\n  AND IF " in slot1
    assert "\nTHEN " in slot1


def test_summarize_chain() -> None:
    evt = event_id_user_macro_button(5)
    chain = ClausalChain(
        head=Program(
            slot=1, prog_type=int(ProgramType.WHEN),
            month=(evt >> 8) & 0xFF, day=evt & 0xFF,
        ),
        conditions=(
            _and_traditional(2, family=0x04, instance=7),
            _and_traditional(3, family=0x0A, instance=3),
        ),
        actions=(
            _then_record(4, int(Command.UNIT_ON), 9),
            _then_record(5, int(Command.UNIT_OFF), 10),
        ),
    )
    r = _renderer_with(names={
        ("button", 5): "BTN", ("unit", 9): "L1", ("unit", 10): "L2",
    })
    text = tokens_to_string(r.summarize_chain(chain))
    assert text == "WHEN BTN is pressed (+2 cond) → Turn ON L1 (+1 more)"
