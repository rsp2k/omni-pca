"""Structured-English rendering of HAI Omni panel programs.

The decoded :class:`omni_pca.programs.Program` records produced by
``pca_file`` and the wire upload paths carry every byte but no narrative.
This module turns them into readable sentences modelled on PC Access's
program editor:

    WHEN Front Door is opened
      AND IF Living Room Motion is secure
      AND IF after sunset
        OR IF Bedtime Mode is active
    THEN Turn ON Hallway Light
     AND Show Message "WELCOME HOME"

Output is a sequence of :class:`Token` records rather than a flat string
so that consumers (CLI, HA frontend, anything else) can:

* Identify object references (zones / units / areas / thermostats /
  buttons / messages) — render each as a clickable link to the entity
  page, badge them with live state, etc.
* Style keywords (`WHEN`, `AND IF`, `THEN`) separately from object
  names and values.
* Recover plain text trivially via ``"".join(t.text for t in tokens)``.

A :class:`ProgramRenderer` is constructed with a :class:`NameResolver`
and an optional :class:`StateResolver` for the live-state overlay. The
two resolvers are protocols (any object with the right methods works);
the convenience :class:`AccountNameResolver` adapts a :class:`PcaAccount`
and :class:`MockStateResolver` adapts a :class:`MockState` — together
those cover the two common consumers (offline ``.pca`` snapshot vs.
running mock panel) without forcing either into a base class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Protocol, runtime_checkable

from .commands import Command
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
from .program_engine import (
    ClausalChain,
    EVENT_AC_POWER_OFF,
    EVENT_AC_POWER_ON,
    EVENT_PHONE_DEAD,
    EVENT_PHONE_OFF_HOOK,
    EVENT_PHONE_ON_HOOK,
    EVENT_PHONE_RINGING,
)


# --------------------------------------------------------------------------
# Token stream
# --------------------------------------------------------------------------


class TokenKind:
    """String constants for :attr:`Token.kind`. Defined as a class of
    str constants so consumers can do ``if t.kind == TokenKind.REF``."""

    KEYWORD: str = "keyword"     # WHEN, AND IF, THEN, OR IF, etc.
    OPERATOR: str = "operator"   # is, ==, >, after, before, …
    REF: str = "ref"             # an object reference (zone / unit / …)
    VALUE: str = "value"         # a literal value (time, number, mode name)
    TEXT: str = "text"           # plain prose connectors
    INDENT: str = "indent"       # leading whitespace for the next line
    NEWLINE: str = "newline"     # end-of-line


@dataclass(frozen=True, slots=True)
class Token:
    """One unit of structured-English output.

    ``text`` is what the consumer prints. The other fields are
    metadata; only ``REF`` tokens use them all.

    For ``REF`` tokens:
        * ``entity_kind`` is one of ``"zone"`` / ``"unit"`` / ``"area"``
          / ``"thermostat"`` / ``"button"`` / ``"message"``
          / ``"code"`` / ``"timeclock"``
        * ``entity_id`` is the 1-based slot the reference resolves to
        * ``state`` is the live-state overlay string when a state
          resolver was provided (e.g. ``"SECURE"``, ``"ON 60%"``,
          ``"Off"``); ``None`` when no overlay is available

    For non-REF tokens, ``entity_kind`` / ``entity_id`` / ``state`` are ``None``.
    """

    kind: str
    text: str
    entity_kind: str | None = None
    entity_id: int | None = None
    state: str | None = None


def tokens_to_string(tokens: Iterable[Token]) -> str:
    """Render a token stream to plain text. Useful for logs / dumps."""
    pieces: list[str] = []
    for t in tokens:
        if t.kind == TokenKind.NEWLINE:
            pieces.append("\n")
        elif t.kind == TokenKind.INDENT:
            pieces.append(t.text)
        else:
            pieces.append(t.text)
            if t.kind == TokenKind.REF and t.state is not None:
                pieces.append(f" [{t.state}]")
    return "".join(pieces)


# --------------------------------------------------------------------------
# Resolver protocols + default implementations
# --------------------------------------------------------------------------


@runtime_checkable
class NameResolver(Protocol):
    """Translate a (kind, 1-based-index) reference into a human name.

    Returns the name string when known, or ``None`` when the slot is
    undefined / the kind isn't supported. The renderer falls back to
    a generated label (``"Zone 5"``, ``"Unit 7"``) when the resolver
    returns ``None``.
    """

    def name_of(self, kind: str, index: int) -> str | None: ...


@runtime_checkable
class StateResolver(Protocol):
    """Translate a (kind, 1-based-index) reference into a live-state
    overlay string. Returns ``None`` when no overlay applies — the
    renderer omits the bracketed annotation in that case.
    """

    def state_of(self, kind: str, index: int) -> str | None: ...


class AccountNameResolver:
    """Resolves names from a :class:`omni_pca.pca_file.PcaAccount`.

    Works as both a static-snapshot view (offline ``.pca`` inspection)
    and as a fallback for the HA path when only header data is loaded.
    """

    def __init__(self, account) -> None:
        self._account = account

    def name_of(self, kind: str, index: int) -> str | None:
        table = {
            "zone": getattr(self._account, "zone_names", {}),
            "unit": getattr(self._account, "unit_names", {}),
            "area": getattr(self._account, "area_names", {}),
            "thermostat": getattr(self._account, "thermostat_names", {}),
            "button": getattr(self._account, "button_names", {}),
            "message": getattr(self._account, "message_names", {}),
            "code": getattr(self._account, "code_names", {}),
        }.get(kind, {})
        return table.get(index)


class MockStateResolver:
    """Resolves both names and live state from a :class:`MockState`.

    Implements both :class:`NameResolver` and :class:`StateResolver`
    so the same object covers both roles when rendering against a
    running mock panel.
    """

    def __init__(self, state) -> None:
        self._state = state

    def name_of(self, kind: str, index: int) -> str | None:
        getter = {
            "zone": getattr(self._state, "zones", {}).get,
            "unit": getattr(self._state, "units", {}).get,
            "area": getattr(self._state, "areas", {}).get,
            "thermostat": getattr(self._state, "thermostats", {}).get,
            "button": getattr(self._state, "buttons", {}).get,
        }.get(kind)
        if getter is None:
            return None
        obj = getter(index)
        return getattr(obj, "name", None) if obj else None

    def state_of(self, kind: str, index: int) -> str | None:
        if kind == "zone":
            z = self._state.zones.get(index)
            if z is None:
                return None
            if z.is_bypassed:
                return "BYPASSED"
            return "NOT READY" if z.current_state != 0 else "SECURE"
        if kind == "unit":
            u = self._state.units.get(index)
            if u is None:
                return None
            if u.state == 0:
                return "OFF"
            if u.state >= 100:
                return f"ON {u.state - 100}%"
            return "ON"
        if kind == "area":
            a = self._state.areas.get(index)
            if a is None:
                return None
            return _SECURITY_MODE_NAMES.get(a.mode, f"mode {a.mode}")
        if kind == "thermostat":
            t = self._state.thermostats.get(index)
            if t is None or t.temperature_raw == 0:
                return None
            # Linear scale on Omni: temp_raw / 2 - 40 = °F.
            return f"{t.temperature_raw // 2 - 40}°F"
        return None


_SECURITY_MODE_NAMES: dict[int, str] = {
    0: "Off",
    1: "Day",
    2: "Night",
    3: "Away",
    4: "Vacation",
    5: "Day Instant",
    6: "Night Delayed",
}


# --------------------------------------------------------------------------
# Helpers — friendly names for fixed enums
# --------------------------------------------------------------------------


_DAY_BIT_LABELS: tuple[tuple[int, str], ...] = (
    (int(Days.MONDAY), "Mon"),
    (int(Days.TUESDAY), "Tue"),
    (int(Days.WEDNESDAY), "Wed"),
    (int(Days.THURSDAY), "Thu"),
    (int(Days.FRIDAY), "Fri"),
    (int(Days.SATURDAY), "Sat"),
    (int(Days.SUNDAY), "Sun"),
)

_ALL_DAYS_MASK: int = sum(b for b, _ in _DAY_BIT_LABELS)
_WEEKDAYS_MASK: int = int(
    Days.MONDAY | Days.TUESDAY | Days.WEDNESDAY | Days.THURSDAY | Days.FRIDAY
)
_WEEKEND_MASK: int = int(Days.SATURDAY | Days.SUNDAY)


def format_days(mask: int) -> str:
    """Render a Days bitmask as a friendly schedule string.

    Common patterns get short names; anything else is the abbreviated
    weekday list (``"Mon, Wed, Fri"``).
    """
    if mask == 0:
        return "never"
    if mask & _ALL_DAYS_MASK == _ALL_DAYS_MASK:
        return "every day"
    if mask & _ALL_DAYS_MASK == _WEEKDAYS_MASK:
        return "weekdays"
    if mask & _ALL_DAYS_MASK == _WEEKEND_MASK:
        return "weekends"
    parts = [label for bit, label in _DAY_BIT_LABELS if mask & bit]
    return ", ".join(parts) if parts else "(no days)"


# Command → ("verb", expects_pr2_object_kind) lookup. ``None`` for the
# second element means "no object reference" — the command's parameters
# are the action's payload alone.
_COMMAND_VERBS: dict[int, tuple[str, str | None]] = {
    int(Command.UNIT_OFF):     ("Turn OFF",       "unit"),
    int(Command.UNIT_ON):      ("Turn ON",        "unit"),
    int(Command.ALL_OFF):      ("Turn ALL OFF",   None),
    int(Command.ALL_ON):       ("Turn ALL ON",    None),
    int(Command.BYPASS_ZONE):  ("Bypass",         "zone"),
    int(Command.RESTORE_ZONE): ("Restore",        "zone"),
    int(Command.RESTORE_ALL_ZONES): ("Restore all zones", None),
    int(Command.EXECUTE_BUTTON): ("Execute button", "button"),
    int(Command.UNIT_LEVEL):   ("Set level",      "unit"),
    int(Command.UNIT_RAMP):    ("Ramp",           "unit"),
    int(Command.DIM_STEP):     ("Dim",            "unit"),
    int(Command.BRIGHT_STEP):  ("Brighten",       "unit"),
    int(Command.SECURITY_OFF): ("Disarm",         "area"),
    int(Command.SECURITY_DAY): ("Arm Day",        "area"),
    int(Command.SECURITY_NIGHT): ("Arm Night",    "area"),
    int(Command.SECURITY_AWAY): ("Arm Away",      "area"),
    int(Command.SECURITY_VACATION): ("Arm Vacation", "area"),
    int(Command.SECURITY_DAY_INSTANT): ("Arm Day Instant", "area"),
    int(Command.SECURITY_NIGHT_DELAYED): ("Arm Night Delayed", "area"),
}


# --------------------------------------------------------------------------
# The renderer
# --------------------------------------------------------------------------


@dataclass
class ProgramRenderer:
    """Render :class:`Program` records and clausal chains as token streams.

    Parameters
    ----------
    names:
        Object-name resolver (zones, units, areas, thermostats, buttons,
        messages, codes). Pass an :class:`AccountNameResolver` for
        offline ``.pca`` snapshots or a :class:`MockStateResolver` for
        the mock-panel case.
    state:
        Optional live-state resolver. When provided, every ``REF`` token
        carries a ``state`` annotation that consumers can render as a
        badge (``"Front Door [SECURE]"`` etc.).
    """

    names: NameResolver
    state: StateResolver | None = None

    # ---- public API ------------------------------------------------------

    def render_program(self, p: Program) -> list[Token]:
        """Render a single compact-form program (TIMED / EVENT / YEARLY).

        Returns the multi-line full form. For a one-line summary, see
        :meth:`summarize_program`.
        """
        out: list[Token] = []
        try:
            kind = ProgramType(p.prog_type)
        except ValueError:
            out.append(Token(TokenKind.TEXT, f"Unknown program type {p.prog_type}"))
            return out
        if kind == ProgramType.TIMED:
            self._emit_timed_header(p, out)
        elif kind == ProgramType.EVENT:
            self._emit_event_header(p, out)
        elif kind == ProgramType.YEARLY:
            self._emit_yearly_header(p, out)
        elif kind == ProgramType.REMARK:
            self._emit_remark(p, out)
            return out
        elif kind == ProgramType.FREE:
            out.append(Token(TokenKind.TEXT, "(empty slot)"))
            return out
        else:
            # Multi-record record on its own — caller should use
            # render_chain instead. Be helpful rather than silent.
            out.append(Token(
                TokenKind.TEXT,
                f"(multi-record {kind.name} — render with render_chain)",
            ))
            return out
        # Compact-form programs can carry up to two inline AND conditions
        # in their cond / cond2 fields. Skip when both are zero.
        for slot_idx, field_val in (("cond", p.cond), ("cond2", p.cond2)):
            if field_val == 0:
                continue
            out.append(Token(TokenKind.NEWLINE, ""))
            out.append(Token(TokenKind.INDENT, "  "))
            out.append(Token(TokenKind.KEYWORD, "AND IF"))
            out.append(Token(TokenKind.TEXT, " "))
            self._emit_traditional_cond(field_val, out)
        out.append(Token(TokenKind.NEWLINE, ""))
        out.append(Token(TokenKind.KEYWORD, "THEN"))
        out.append(Token(TokenKind.TEXT, " "))
        self._emit_action(p, out)
        return out

    def render_chain(self, chain: ClausalChain) -> list[Token]:
        """Render a multi-record clausal chain (WHEN/AT/EVERY + body).

        Output mirrors PC Access's structured-English: trigger on the
        first line, conditions indented two spaces with ``AND IF`` /
        ``OR IF`` keywords, actions on their own lines under ``THEN`` /
        ``AND``.
        """
        out: list[Token] = []
        head = chain.head
        head_kind = head.prog_type
        if head_kind == int(ProgramType.WHEN):
            self._emit_when_header(head, out)
        elif head_kind == int(ProgramType.AT):
            self._emit_at_header(head, out)
        elif head_kind == int(ProgramType.EVERY):
            self._emit_every_header(head, out)
        else:
            out.append(Token(TokenKind.TEXT, f"(chain head type {head_kind}?)"))
        # Conditions: AND IF / OR IF, indented.
        for cond in chain.conditions:
            out.append(Token(TokenKind.NEWLINE, ""))
            out.append(Token(TokenKind.INDENT, "  "))
            keyword = "OR IF" if cond.prog_type == int(ProgramType.OR) else "AND IF"
            out.append(Token(TokenKind.KEYWORD, keyword))
            out.append(Token(TokenKind.TEXT, " "))
            self._emit_and_record(cond, out)
        # Actions: first one prefixed THEN, rest AND.
        for i, action in enumerate(chain.actions):
            out.append(Token(TokenKind.NEWLINE, ""))
            out.append(Token(TokenKind.KEYWORD, "THEN" if i == 0 else "AND"))
            out.append(Token(TokenKind.TEXT, " "))
            self._emit_action(action, out)
        return out

    def summarize_program(self, p: Program) -> list[Token]:
        """One-line summary suitable for the list view.

        Format: ``<trigger summary> → <action summary>``. Conditions
        on compact-form programs are elided with ``(+N conds)``.
        """
        out: list[Token] = []
        try:
            kind = ProgramType(p.prog_type)
        except ValueError:
            out.append(Token(TokenKind.TEXT, f"?type {p.prog_type}"))
            return out
        if kind == ProgramType.TIMED:
            self._emit_timed_summary(p, out)
        elif kind == ProgramType.EVENT:
            self._emit_event_summary(p, out)
        elif kind == ProgramType.YEARLY:
            self._emit_yearly_summary(p, out)
        elif kind == ProgramType.REMARK:
            self._emit_remark(p, out)
            return out
        elif kind == ProgramType.FREE:
            out.append(Token(TokenKind.TEXT, "(empty)"))
            return out
        else:
            out.append(Token(TokenKind.TEXT, kind.name))
            return out
        # Inline condition count.
        cond_count = (1 if p.cond else 0) + (1 if p.cond2 else 0)
        if cond_count:
            out.append(Token(TokenKind.TEXT, f" (+{cond_count} cond)"))
        out.append(Token(TokenKind.TEXT, " → "))
        self._emit_action(p, out)
        return out

    def summarize_chain(self, chain: ClausalChain) -> list[Token]:
        """One-line summary of a clausal chain for the list view."""
        out: list[Token] = []
        head = chain.head
        if head.prog_type == int(ProgramType.WHEN):
            out.append(Token(TokenKind.KEYWORD, "WHEN"))
            out.append(Token(TokenKind.TEXT, " "))
            self._emit_event(head.event_id, out)
        elif head.prog_type == int(ProgramType.AT):
            out.append(Token(TokenKind.KEYWORD, "AT"))
            out.append(Token(TokenKind.TEXT, " "))
            out.append(Token(TokenKind.VALUE, head.format_time()))
            out.append(Token(TokenKind.TEXT, " "))
            out.append(Token(TokenKind.VALUE, format_days(head.days)))
        elif head.prog_type == int(ProgramType.EVERY):
            out.append(Token(TokenKind.KEYWORD, "EVERY"))
            out.append(Token(TokenKind.TEXT, " "))
            out.append(Token(TokenKind.VALUE, _format_interval(head.every_interval)))
        if chain.conditions:
            out.append(Token(
                TokenKind.TEXT,
                f" (+{len(chain.conditions)} cond)",
            ))
        out.append(Token(TokenKind.TEXT, " → "))
        # Show the first action only on summary; "+N more" if there are more.
        if chain.actions:
            self._emit_action(chain.actions[0], out)
            if len(chain.actions) > 1:
                out.append(Token(
                    TokenKind.TEXT,
                    f" (+{len(chain.actions) - 1} more)",
                ))
        return out

    # ---- emit helpers — triggers / headers -------------------------------

    def _emit_timed_header(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "AT"))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.VALUE, p.format_time()))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.VALUE, format_days(p.days)))

    def _emit_event_header(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "WHEN"))
        out.append(Token(TokenKind.TEXT, " "))
        self._emit_event(p.event_id, out)

    def _emit_yearly_header(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "ON"))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(
            TokenKind.VALUE, f"{p.month:d}/{p.day:d} at {p.hour:02d}:{p.minute:02d}",
        ))

    def _emit_remark(self, p: Program, out: list[Token]) -> None:
        rid = p.remark_id if p.remark_id is not None else 0
        out.append(Token(TokenKind.KEYWORD, "REMARK"))
        out.append(Token(TokenKind.TEXT, f" #{rid}"))

    def _emit_when_header(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "WHEN"))
        out.append(Token(TokenKind.TEXT, " "))
        self._emit_event(p.event_id, out)

    def _emit_at_header(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "AT"))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.VALUE, p.format_time()))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.VALUE, format_days(p.days)))

    def _emit_every_header(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "EVERY"))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.VALUE, _format_interval(p.every_interval)))

    def _emit_timed_summary(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.VALUE, p.format_time()))
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.VALUE, format_days(p.days)))

    def _emit_event_summary(self, p: Program, out: list[Token]) -> None:
        out.append(Token(TokenKind.KEYWORD, "WHEN"))
        out.append(Token(TokenKind.TEXT, " "))
        self._emit_event(p.event_id, out)

    def _emit_yearly_summary(self, p: Program, out: list[Token]) -> None:
        out.append(Token(
            TokenKind.VALUE,
            f"{p.month:d}/{p.day:d} @ {p.hour:02d}:{p.minute:02d}",
        ))

    def _emit_event(self, event_id: int, out: list[Token]) -> None:
        """Render an event-ID as natural language.

        Mirrors clsText.GetEventCategory (clsText.cs:1585-...) for the
        common categories. Unknown event IDs render as ``"event 0xNNNN"``.
        """
        if event_id == EVENT_PHONE_DEAD:
            out.append(Token(TokenKind.TEXT, "phone line is dead"))
            return
        if event_id == EVENT_PHONE_RINGING:
            out.append(Token(TokenKind.TEXT, "phone is ringing"))
            return
        if event_id == EVENT_PHONE_OFF_HOOK:
            out.append(Token(TokenKind.TEXT, "phone is off hook"))
            return
        if event_id == EVENT_PHONE_ON_HOOK:
            out.append(Token(TokenKind.TEXT, "phone is on hook"))
            return
        if event_id == EVENT_AC_POWER_OFF:
            out.append(Token(TokenKind.TEXT, "AC power lost"))
            return
        if event_id == EVENT_AC_POWER_ON:
            out.append(Token(TokenKind.TEXT, "AC power restored"))
            return
        # USER_MACRO_BUTTON (high byte == 0)
        if (event_id & 0xFF00) == 0x0000:
            button = event_id & 0xFF
            self._emit_ref("button", button, out)
            out.append(Token(TokenKind.TEXT, " is pressed"))
            return
        # ZONE_STATE_CHANGE (& 0xFC00 == 0x0400)
        if (event_id & 0xFC00) == 0x0400:
            zone_state = event_id & 0x03FF
            zone = (zone_state // 4) + 1
            state = zone_state % 4
            self._emit_ref("zone", zone, out)
            state_label = {
                0: "becomes secure",
                1: "becomes not ready",
                2: "reports trouble",
                3: "reports tamper",
            }.get(state, f"changes to state {state}")
            out.append(Token(TokenKind.TEXT, " " + state_label))
            return
        # UNIT_STATE_CHANGE (& 0xFC00 == 0x0800)
        if (event_id & 0xFC00) == 0x0800:
            unit_state = event_id & 0x03FF
            unit = (unit_state // 2) + 1
            on = unit_state & 1
            self._emit_ref("unit", unit, out)
            out.append(Token(TokenKind.TEXT, " turns " + ("ON" if on else "OFF")))
            return
        out.append(Token(TokenKind.TEXT, f"event 0x{event_id:04x}"))

    # ---- emit helpers — conditions ---------------------------------------

    def _emit_traditional_cond(self, cond: int, out: list[Token]) -> None:
        """Render a compact-form ``cond`` u16 (TIMED/EVENT/YEARLY inline
        AND condition).

        These use a different bit-layout from AND-record cond fields —
        see clsText.GetConditionalText (clsText.cs:2224-2274).
        """
        family = (cond >> 8) & 0xFC
        if family == 0:
            misc = cond & 0x0F
            self._emit_misc_conditional(misc, out)
            return
        if family == ProgramCond.ZONE:
            zone = cond & 0xFF
            not_ready = bool(cond & 0x0200)
            self._emit_ref("zone", zone, out)
            out.append(Token(TokenKind.TEXT, " is "))
            out.append(Token(TokenKind.OPERATOR, "not ready" if not_ready else "secure"))
            return
        if family == ProgramCond.CTRL:
            unit = cond & 0x01FF
            on = bool(cond & 0x0200)
            self._emit_ref("unit", unit, out)
            out.append(Token(TokenKind.TEXT, " is "))
            out.append(Token(TokenKind.OPERATOR, "ON" if on else "OFF"))
            return
        if family == ProgramCond.TIME:
            tc = cond & 0xFF
            enabled = bool(cond & 0x0200)
            out.append(Token(TokenKind.TEXT, "Time clock "))
            out.append(Token(TokenKind.VALUE, str(tc)))
            out.append(Token(TokenKind.TEXT, " is "))
            out.append(Token(TokenKind.OPERATOR,
                             "enabled" if enabled else "disabled"))
            return
        # SEC default: high nibble = mode, bits 8-11 = area.
        area = (cond >> 8) & 0x0F
        mode = (cond >> 12) & 0x07
        if area == 0:
            area = 1
        self._emit_ref("area", area, out)
        out.append(Token(TokenKind.TEXT, " is "))
        out.append(Token(
            TokenKind.VALUE,
            _SECURITY_MODE_NAMES.get(mode, f"mode {mode}"),
        ))

    def _emit_and_record(self, c: Program, out: list[Token]) -> None:
        """Render an AND/OR Program record (Traditional or Structured)."""
        if c.and_op == CondOP.ARG1_TRADITIONAL:
            self._emit_traditional_and(c, out)
        else:
            self._emit_structured_and(c, out)

    def _emit_traditional_and(self, c: Program, out: list[Token]) -> None:
        """AND/OR record carrying a Traditional condition.

        Encoding via clsConditionLine.Cond (clsConditionLine.cs:17-33):
        ``and_family`` is the family+selector byte; ``and_instance`` is
        the object index (1-based).
        """
        family = c.and_family
        instance = c.and_instance
        family_major = family & 0xFC
        secondary = bool(family & 0x02)
        if family_major == 0:
            self._emit_misc_conditional(family & 0x0F, out)
            return
        if family_major == ProgramCond.ZONE:
            self._emit_ref("zone", instance, out)
            out.append(Token(TokenKind.TEXT, " is "))
            out.append(Token(
                TokenKind.OPERATOR, "not ready" if secondary else "secure",
            ))
            return
        if family_major == ProgramCond.CTRL:
            self._emit_ref("unit", instance, out)
            out.append(Token(TokenKind.TEXT, " is "))
            out.append(Token(
                TokenKind.OPERATOR, "ON" if secondary else "OFF",
            ))
            return
        if family_major == ProgramCond.TIME:
            out.append(Token(TokenKind.TEXT, "Time clock "))
            out.append(Token(TokenKind.VALUE, str(instance)))
            out.append(Token(TokenKind.TEXT, " is "))
            out.append(Token(
                TokenKind.OPERATOR,
                "enabled" if secondary else "disabled",
            ))
            return
        # SEC: high nibble = mode, low = area
        area = family & 0x0F
        mode = (family >> 4) & 0x07
        if area == 0:
            area = 1
        self._emit_ref("area", area, out)
        out.append(Token(TokenKind.TEXT, " is "))
        out.append(Token(
            TokenKind.VALUE,
            _SECURITY_MODE_NAMES.get(mode, f"mode {mode}"),
        ))

    def _emit_misc_conditional(self, misc_code: int, out: list[Token]) -> None:
        try:
            cat = MiscConditional(misc_code)
        except ValueError:
            out.append(Token(TokenKind.TEXT, f"misc condition {misc_code}"))
            return
        labels = {
            MiscConditional.NONE: "always",
            MiscConditional.NEVER: "never",
            MiscConditional.LIGHT: "it is light outside",
            MiscConditional.DARK: "it is dark outside",
            MiscConditional.PHONE_DEAD: "phone line is dead",
            MiscConditional.PHONE_RINGING: "phone is ringing",
            MiscConditional.PHONE_OFF_HOOK: "phone is off hook",
            MiscConditional.PHONE_ON_HOOK: "phone is on hook",
            MiscConditional.AC_POWER_OFF: "AC power is off",
            MiscConditional.AC_POWER_ON: "AC power is on",
            MiscConditional.BATTERY_LOW: "battery is low",
            MiscConditional.BATTERY_OK: "battery is OK",
            MiscConditional.ENERGY_COST_LOW: "energy cost is low",
            MiscConditional.ENERGY_COST_MID: "energy cost is mid",
            MiscConditional.ENERGY_COST_HIGH: "energy cost is high",
            MiscConditional.ENERGY_COST_CRITICAL: "energy cost is critical",
        }
        out.append(Token(TokenKind.TEXT, labels.get(cat, cat.name)))

    def _emit_structured_and(self, c: Program, out: list[Token]) -> None:
        """Render an ``Arg1 OP Arg2`` AND/OR record.

        For each arg side we render either an object reference + field,
        or a literal value. The operator goes in between.
        """
        self._emit_structured_arg(
            c.and_arg1_argtype, c.and_arg1_ix, c.and_arg1_field, out,
        )
        out.append(Token(TokenKind.TEXT, " "))
        out.append(Token(TokenKind.OPERATOR, _OP_SYMBOLS.get(c.and_op, "?")))
        out.append(Token(TokenKind.TEXT, " "))
        self._emit_structured_arg(
            c.and_arg2_argtype, c.and_arg2_ix, c.and_arg2_field, out,
        )

    def _emit_structured_arg(
        self, argtype: int, ix: int, field_id: int, out: list[Token],
    ) -> None:
        if argtype == CondArgType.CONSTANT:
            out.append(Token(TokenKind.VALUE, str(ix)))
            return
        kind = _ARGTYPE_KIND.get(argtype)
        if kind is None:
            out.append(Token(TokenKind.TEXT, f"argtype{argtype}#{ix}"))
            return
        if kind == "timedate":
            field_label = _TIMEDATE_FIELD_LABELS.get(field_id, f"field{field_id}")
            out.append(Token(TokenKind.TEXT, field_label))
            return
        # Object reference with field suffix (when known).
        self._emit_ref(kind, ix, out)
        field_label = _FIELD_LABELS.get((kind, field_id))
        if field_label:
            out.append(Token(TokenKind.TEXT, "."))
            out.append(Token(TokenKind.TEXT, field_label))

    # ---- emit helpers — actions ------------------------------------------

    def _emit_action(self, p: Program, out: list[Token]) -> None:
        """Render the cmd / par / pr2 triple as a friendly verb.

        For unrecognised commands we fall back to the raw enum name,
        which keeps the rendering useful even for less-common
        Command values we haven't mapped yet.
        """
        cmd_byte = p.cmd
        try:
            cmd = Command(cmd_byte)
        except ValueError:
            out.append(Token(TokenKind.TEXT, f"command {cmd_byte}"))
            return
        verb_entry = _COMMAND_VERBS.get(cmd_byte)
        verb, ref_kind = verb_entry if verb_entry else (cmd.name.replace("_", " "), None)
        out.append(Token(TokenKind.KEYWORD, verb))
        if ref_kind is not None:
            out.append(Token(TokenKind.TEXT, " "))
            self._emit_ref(ref_kind, p.pr2, out)
        if cmd == Command.UNIT_LEVEL:
            out.append(Token(TokenKind.TEXT, " to "))
            out.append(Token(TokenKind.VALUE, f"{p.par}%"))

    # ---- emit helpers — refs ---------------------------------------------

    def _emit_ref(self, kind: str, index: int, out: list[Token]) -> None:
        """Emit a typed object reference token with name + live state."""
        name = self.names.name_of(kind, index)
        if not name:
            name = f"{kind.capitalize()} {index}"
        state = None
        if self.state is not None:
            state = self.state.state_of(kind, index)
        out.append(Token(
            TokenKind.REF, name,
            entity_kind=kind, entity_id=index, state=state,
        ))


# --------------------------------------------------------------------------
# Tables — kept at module scope so they're not re-allocated per render
# --------------------------------------------------------------------------


_OP_SYMBOLS: dict[int, str] = {
    int(CondOP.ARG1_EQ_ARG2): "==",
    int(CondOP.ARG1_NE_ARG2): "!=",
    int(CondOP.ARG1_LT_ARG2): "<",
    int(CondOP.ARG1_GT_ARG2): ">",
    int(CondOP.ARG1_ODD): "is odd",
    int(CondOP.ARG1_EVEN): "is even",
    int(CondOP.ARG1_MULTIPLE_ARG2): "is multiple of",
    int(CondOP.ARG1_IN_ARG2): "in",
    int(CondOP.ARG1_NOT_IN_ARG2): "not in",
}


_ARGTYPE_KIND: dict[int, str] = {
    int(CondArgType.ZONE): "zone",
    int(CondArgType.UNIT): "unit",
    int(CondArgType.THERMOSTAT): "thermostat",
    int(CondArgType.AREA): "area",
    int(CondArgType.TIME_DATE): "timedate",
}


_FIELD_LABELS: dict[tuple[str, int], str] = {
    # enuZoneField
    ("zone", 1): "LoopReading",
    ("zone", 2): "CurrentState",
    ("zone", 3): "ArmingState",
    ("zone", 4): "AlarmState",
    # enuUnitField
    ("unit", 1): "CurrentState",
    ("unit", 2): "PreviousState",
    ("unit", 3): "Timer",
    ("unit", 4): "Level",
    # enuThermostatField
    ("thermostat", 1): "Temperature",
    ("thermostat", 2): "HeatSetpoint",
    ("thermostat", 3): "CoolSetpoint",
    ("thermostat", 4): "SystemMode",
    ("thermostat", 5): "FanMode",
    ("thermostat", 6): "HoldMode",
    ("thermostat", 7): "FreezeAlarm",
    ("thermostat", 8): "CommError",
    ("thermostat", 9): "Humidity",
    ("thermostat", 10): "HumidifySetpoint",
    ("thermostat", 11): "DehumidifySetpoint",
    ("thermostat", 12): "OutdoorTemperature",
    ("thermostat", 13): "SystemStatus",
}


_TIMEDATE_FIELD_LABELS: dict[int, str] = {
    1: "Date",
    2: "Year",
    3: "Month",
    4: "Day",
    5: "DayOfWeek",
    6: "Time",
    7: "DST_Flag",
    8: "Hour",
    9: "Minute",
    10: "SunriseSunset",
}


def _format_interval(seconds: int) -> str:
    """Render an EVERY-program interval. Treats the raw value as
    seconds — matches the live-fixture observation that 5 SECONDS UI
    selection stores as 5. Higher values fall through to natural
    ``"30 min"`` / ``"2 hr"`` shortenings for readability."""
    if seconds <= 0:
        return "(disabled)"
    if seconds < 60:
        return f"{seconds} sec"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} hr"
