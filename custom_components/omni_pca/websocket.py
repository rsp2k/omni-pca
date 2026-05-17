"""HA websocket commands for the program viewer.

The frontend talks to the integration via Home Assistant's standard
websocket API. We register three commands here, all namespaced under
``omni_pca/programs/``:

* ``omni_pca/programs/list`` — paginated, filterable summary list. Each
  result row carries the token stream for the one-line summary plus the
  metadata the frontend needs to filter and drill in.
* ``omni_pca/programs/get`` — full detail for a single slot. Returns
  the structured-English token stream for the compact form or the
  full clausal chain.
* ``omni_pca/programs/fire`` — send ``Command.EXECUTE_PROGRAM`` over
  the wire to ask the panel to run a program now. Returns success/error.

All commands take an ``entry_id`` so multi-panel installs can address
the right coordinator. The frontend's panel UI uses HA's `<ha-conn>`
WS client; this module just produces JSON-safe dicts.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from omni_pca.commands import Command
from omni_pca.program_engine import ClausalChain, build_chains
from omni_pca.program_renderer import (
    NameResolver,
    ProgramRenderer,
    StateResolver,
    Token,
)
from omni_pca.programs import Program, ProgramType

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import OmniDataUpdateCoordinator


# --------------------------------------------------------------------------
# Coordinator-backed resolvers
# --------------------------------------------------------------------------


class _CoordinatorNameResolver:
    """Resolve object names from coordinator-discovered topology.

    The coordinator's :class:`OmniData` stores ``zones`` / ``units`` /
    ``areas`` / ``thermostats`` / ``buttons`` as dicts of typed
    properties dataclasses (each with a ``name`` attribute). For
    ``message`` / ``code`` / ``timeclock`` we don't track HA-side
    properties — fall through to ``None`` so the renderer generates
    ``"Message 5"``-style labels.
    """

    def __init__(self, coordinator: "OmniDataUpdateCoordinator") -> None:
        self._coordinator = coordinator

    def name_of(self, kind: str, index: int) -> str | None:
        data = self._coordinator.data
        if data is None:
            return None
        bucket = {
            "zone": data.zones,
            "unit": data.units,
            "area": data.areas,
            "thermostat": data.thermostats,
            "button": data.buttons,
        }.get(kind)
        if bucket is None:
            return None
        props = bucket.get(index)
        if props is None:
            return None
        return getattr(props, "name", None) or None


class _CoordinatorStateResolver:
    """Live-state overlay using the coordinator's *_status maps.

    The status dicts update on every poll *and* are patched in-place
    when the event listener decodes a push event, so each websocket
    call sees the freshest available state without a round-trip to
    the panel.
    """

    _AREA_MODES: dict[int, str] = {
        0: "Off", 1: "Day", 2: "Night", 3: "Away",
        4: "Vacation", 5: "Day Instant", 6: "Night Delayed",
    }

    def __init__(self, coordinator: "OmniDataUpdateCoordinator") -> None:
        self._coordinator = coordinator

    def state_of(self, kind: str, index: int) -> str | None:
        data = self._coordinator.data
        if data is None:
            return None
        if kind == "zone":
            status = data.zone_status.get(index)
            if status is None:
                return None
            if status.is_bypassed:
                return "BYPASSED"
            return {0: "SECURE", 1: "NOT READY", 2: "TROUBLE", 3: "TAMPER"}.get(
                status.current_state, f"state {status.current_state}",
            )
        if kind == "unit":
            status = data.unit_status.get(index)
            if status is None:
                return None
            if status.state == 0:
                return "OFF"
            if status.state >= 100:
                return f"ON {status.state - 100}%"
            return "ON"
        if kind == "area":
            status = data.area_status.get(index)
            if status is None:
                return None
            return self._AREA_MODES.get(status.mode, f"mode {status.mode}")
        if kind == "thermostat":
            status = data.thermostat_status.get(index)
            if status is None or status.temperature_raw == 0:
                return None
            return f"{status.temperature_raw // 2 - 40}°F"
        return None


# --------------------------------------------------------------------------
# Token serialisation + reference extraction
# --------------------------------------------------------------------------


def _tokens_to_json(tokens: list[Token]) -> list[dict[str, Any]]:
    """Serialise a Token list to plain dicts the websocket layer can JSON.

    ``dataclasses.asdict`` would also work but produces ``None`` keys for
    fields irrelevant to the token's kind; we omit those explicitly so
    the wire format stays compact and the frontend sees clean shapes.
    """
    out: list[dict[str, Any]] = []
    for t in tokens:
        d: dict[str, Any] = {"k": t.kind, "t": t.text}
        if t.entity_kind is not None:
            d["ek"] = t.entity_kind
        if t.entity_id is not None:
            d["ei"] = t.entity_id
        if t.state is not None:
            d["s"] = t.state
        out.append(d)
    return out


def _extract_references(tokens: list[Token]) -> list[str]:
    """Collect distinct ``"<kind>:<id>"`` references from a token stream.

    Used to populate each list-row's ``references`` field so the
    frontend can filter on "involves this entity" without re-parsing
    the tokens. Returns a deduplicated, stable-ordered list.
    """
    seen: dict[str, None] = {}
    for t in tokens:
        if t.entity_kind and t.entity_id is not None:
            seen[f"{t.entity_kind}:{t.entity_id}"] = None
    return list(seen.keys())


# --------------------------------------------------------------------------
# Helper: pick a coordinator + build the renderer
# --------------------------------------------------------------------------


def _coordinator_for_entry(
    hass: HomeAssistant, entry_id: str,
) -> "OmniDataUpdateCoordinator | None":
    return hass.data.get(DOMAIN, {}).get(entry_id)


def _build_renderer(coordinator: "OmniDataUpdateCoordinator") -> ProgramRenderer:
    return ProgramRenderer(
        names=_CoordinatorNameResolver(coordinator),
        state=_CoordinatorStateResolver(coordinator),
    )


def _classify_trigger(p: Program) -> str:
    """Stable string label for the trigger type — used in filter chips."""
    try:
        return ProgramType(p.prog_type).name
    except ValueError:
        return f"UNKNOWN_{p.prog_type}"


# --------------------------------------------------------------------------
# Websocket commands
# --------------------------------------------------------------------------


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/list",
        vol.Required("entry_id"): str,
        vol.Optional("trigger_types"): [str],
        vol.Optional("references_entity"): str,   # e.g. "zone:5"
        vol.Optional("search"): str,
        vol.Optional("limit"): vol.All(int, vol.Range(min=1, max=1500)),
        vol.Optional("offset"): vol.All(int, vol.Range(min=0)),
    }
)
@websocket_api.async_response
async def _ws_list_programs(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Paginated list of programs with filters applied.

    Returns rows containing the one-line summary tokens plus the
    metadata needed for filter UI (trigger type, references). The
    frontend renders the summary inline; clicking a row triggers a
    follow-up ``programs/get`` for the full detail.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return

    renderer = _build_renderer(coordinator)
    programs = coordinator.data.programs if coordinator.data else {}
    # Clausal chains span multiple slots — group them so each chain
    # appears once instead of one row per slot.
    chains_by_head_slot = {
        c.head.slot: c for c in build_chains(tuple(programs.values()))
    }
    consumed_chain_slots: set[int] = set()
    for chain in chains_by_head_slot.values():
        if chain.head.slot is not None:
            consumed_chain_slots.add(chain.head.slot)
        for cond in chain.conditions:
            if cond.slot is not None:
                consumed_chain_slots.add(cond.slot)
        for action in chain.actions:
            if action.slot is not None:
                consumed_chain_slots.add(action.slot)

    rows: list[dict[str, Any]] = []
    for slot in sorted(programs):
        if slot in chains_by_head_slot:
            chain = chains_by_head_slot[slot]
            summary = renderer.summarize_chain(chain)
            rows.append({
                "slot": slot,
                "kind": "chain",
                "trigger_type": _classify_trigger(chain.head),
                "summary": _tokens_to_json(summary),
                "references": _extract_references(summary),
                "condition_count": len(chain.conditions),
                "action_count": len(chain.actions),
            })
            continue
        if slot in consumed_chain_slots:
            continue  # part of a chain we already rendered
        program = programs[slot]
        summary = renderer.summarize_program(program)
        rows.append({
            "slot": slot,
            "kind": "compact",
            "trigger_type": _classify_trigger(program),
            "summary": _tokens_to_json(summary),
            "references": _extract_references(summary),
            "condition_count": (1 if program.cond else 0) + (1 if program.cond2 else 0),
            "action_count": 1,
        })

    # Filtering happens after rendering so the filter UI can use the
    # final, name-resolved text. Trade-off: O(N) per request even when
    # filters narrow the result; with N ≤ 1500 this is fine in practice.
    trigger_types: list[str] | None = msg.get("trigger_types")
    references_entity: str | None = msg.get("references_entity")
    search: str | None = msg.get("search")
    if search:
        search_lower = search.lower()
    filtered = []
    for row in rows:
        if trigger_types and row["trigger_type"] not in trigger_types:
            continue
        if references_entity and references_entity not in row["references"]:
            continue
        if search:
            row_text = "".join(
                tok["t"] for tok in row["summary"] if tok.get("k") != "newline"
            ).lower()
            if search_lower not in row_text:
                continue
        filtered.append(row)

    total = len(rows)
    filtered_total = len(filtered)
    offset = msg.get("offset", 0)
    limit = msg.get("limit", 200)
    page = filtered[offset : offset + limit]

    connection.send_result(msg["id"], {
        "programs": page,
        "total": total,
        "filtered_total": filtered_total,
        "offset": offset,
        "limit": limit,
    })


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/get",
        vol.Required("entry_id"): str,
        vol.Required("slot"): vol.All(int, vol.Range(min=1, max=1500)),
    }
)
@websocket_api.async_response
async def _ws_get_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Full structured-English detail for one slot.

    If the requested slot is the head of a clausal chain we return the
    rendered chain; if it's a continuation slot (an AND/OR/THEN in the
    middle of a chain) we still return the chain that contains it, so
    the frontend always shows the complete program even when the user
    clicks an interior slot.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return

    renderer = _build_renderer(coordinator)
    programs = coordinator.data.programs if coordinator.data else {}
    target = programs.get(msg["slot"])
    if target is None:
        connection.send_error(msg["id"], "not_found", "no program at that slot")
        return

    chains = build_chains(tuple(programs.values()))
    containing_chain: ClausalChain | None = None
    for chain in chains:
        members = (
            (chain.head,) + chain.conditions + chain.actions
        )
        if any(m.slot == msg["slot"] for m in members):
            containing_chain = chain
            break

    if containing_chain is not None:
        tokens = renderer.render_chain(containing_chain)
        connection.send_result(msg["id"], {
            "slot": containing_chain.head.slot,
            "kind": "chain",
            "trigger_type": _classify_trigger(containing_chain.head),
            "tokens": _tokens_to_json(tokens),
            "references": _extract_references(tokens),
            "chain_slots": [m.slot for m in members if m.slot is not None],
            # Per-member raw fields + role so the editor can render
            # an editable form for each line of the clausal chain.
            # role is "head" / "condition" / "action".
            "chain_members": [
                {
                    "slot": m.slot,
                    "role": (
                        "head" if m is containing_chain.head
                        else "action" if m in containing_chain.actions
                        else "condition"
                    ),
                    "fields": _program_to_fields(m),
                }
                for m in members if m.slot is not None
            ],
        })
        return

    tokens = renderer.render_program(target)
    connection.send_result(msg["id"], {
        "slot": msg["slot"],
        "kind": "compact",
        "trigger_type": _classify_trigger(target),
        "tokens": _tokens_to_json(tokens),
        "references": _extract_references(tokens),
        # Raw program fields for the editor to seed its form. The
        # rendered token stream is for *display*; the form needs the
        # underlying integer values to round-trip cleanly.
        "fields": _program_to_fields(target),
    })


def _program_to_fields(program: Program) -> dict[str, Any]:
    """Serialise a Program for the editor form. Mirrors the field
    layout of :func:`_PROGRAM_FIELD_SCHEMA` so a round-trip
    fetch → edit → save is straightforward.
    """
    return {
        "prog_type": program.prog_type,
        "cond": program.cond,
        "cond2": program.cond2,
        "cmd": program.cmd,
        "par": program.par,
        "pr2": program.pr2,
        "month": program.month,
        "day": program.day,
        "days": program.days,
        "hour": program.hour,
        "minute": program.minute,
        "remark_id": program.remark_id,
    }


_PROGRAM_FIELD_SCHEMA = vol.Schema(
    {
        vol.Required("prog_type"): vol.All(int, vol.Range(min=0, max=10)),
        vol.Optional("cond", default=0): vol.All(int, vol.Range(min=0, max=0xFFFF)),
        vol.Optional("cond2", default=0): vol.All(int, vol.Range(min=0, max=0xFFFF)),
        vol.Optional("cmd", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("par", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("pr2", default=0): vol.All(int, vol.Range(min=0, max=0xFFFF)),
        vol.Optional("month", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("day", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("days", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("hour", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("minute", default=0): vol.All(int, vol.Range(min=0, max=0xFF)),
        vol.Optional("remark_id"): vol.Any(None, vol.All(int, vol.Range(min=0))),
    },
    extra=vol.PREVENT_EXTRA,
)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/chain/write",
        vol.Required("entry_id"): str,
        vol.Required("head_slot"): vol.All(int, vol.Range(min=1, max=1500)),
        vol.Required("head"): dict,        # WHEN / AT / EVERY program dict
        vol.Required("conditions"): [dict],
        vol.Required("actions"): [dict],
    }
)
@websocket_api.async_response
async def _ws_chain_write(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Rewrite a clausal chain into consecutive slots.

    A clausal program spans one head (WHEN/AT/EVERY) + N condition
    records (AND/OR) + M action records (THEN), each in its own slot.
    Editing means rewriting the whole run.

    Logic:
      1. Find the *existing* chain that owns ``head_slot`` (so we know
         which old slots to clear when the chain shrinks).
      2. The new run spans slots [head_slot .. head_slot + new_len - 1].
         If new_len > old_len, the additional slots must currently be
         FREE — refuse otherwise so we never trample an adjacent
         program.
      3. Write each new record via ``download_program``. The new run's
         records are emitted in slot order; THEN actions land last.
      4. Clear any old chain slots beyond the new run's end (shrinking
         case) so leftover continuation records don't get mis-associated
         with the now-shorter chain.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return
    try:
        client = coordinator.client
    except RuntimeError as err:
        connection.send_error(msg["id"], "not_connected", str(err))
        return

    from omni_pca.programs import Program  # local — avoid cycle

    # Validate every member dict against the per-record schema (used
    # individually so each member can have its own defaults).
    try:
        head_fields = _PROGRAM_FIELD_SCHEMA(msg["head"])
        condition_fields = [_PROGRAM_FIELD_SCHEMA(c) for c in msg["conditions"]]
        action_fields = [_PROGRAM_FIELD_SCHEMA(a) for a in msg["actions"]]
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", f"bad chain member: {err}")
        return

    if not action_fields:
        connection.send_error(
            msg["id"], "invalid", "chain must have at least one THEN action",
        )
        return

    head_slot = msg["head_slot"]
    new_len = 1 + len(condition_fields) + len(action_fields)

    # Find the existing chain (if any) so we know which old slots are
    # currently part of this program. Without an existing chain we still
    # allow writing — that's the "create chain at this empty slot" case.
    from omni_pca.program_engine import build_chains

    programs = coordinator.data.programs if coordinator.data else {}
    existing = next(
        (c for c in build_chains(tuple(programs.values()))
         if c.head.slot == head_slot),
        None,
    )
    existing_slots: set[int] = set()
    if existing is not None:
        for m in (existing.head, *existing.conditions, *existing.actions):
            if m.slot is not None:
                existing_slots.add(m.slot)

    new_slot_range = range(head_slot, head_slot + new_len)
    if new_slot_range.stop > 1501:
        connection.send_error(
            msg["id"], "invalid",
            f"chain of {new_len} records starting at slot {head_slot} "
            f"would extend past slot 1500",
        )
        return

    # Anti-trample check for any expansion slots that aren't already
    # part of this chain.
    for s in new_slot_range:
        if s in existing_slots:
            continue
        if s in programs and not programs[s].is_empty():
            connection.send_error(
                msg["id"], "invalid",
                f"target slot {s} is occupied by another program "
                f"(slot {s}); free it first",
            )
            return

    # Build the typed records.
    head = Program(slot=head_slot, **head_fields)
    new_records: list[tuple[int, Program]] = [(head_slot, head)]
    for i, cf in enumerate(condition_fields):
        slot = head_slot + 1 + i
        new_records.append((slot, Program(slot=slot, **cf)))
    actions_base = head_slot + 1 + len(condition_fields)
    for i, af in enumerate(action_fields):
        slot = actions_base + i
        new_records.append((slot, Program(slot=slot, **af)))

    # Write them in order.
    try:
        for slot, prog in new_records:
            await client.download_program(slot, prog)
    except NotImplementedError as err:
        connection.send_error(msg["id"], "not_supported", str(err))
        return
    except Exception as err:
        connection.send_error(msg["id"], "write_failed", str(err))
        return

    # Clear any old chain slot that's not in the new range (shrinking
    # case). Order matters: clears come *after* writes so a transient
    # observer never sees a half-rewritten chain.
    to_clear = existing_slots - set(new_slot_range)
    for slot in sorted(to_clear):
        try:
            await client.clear_program(slot)
        except Exception:
            # Don't fail the whole write for a clear-failure; log and continue.
            _log.warning("failed to clear shrunk-away slot %s", slot)

    # Update coordinator state. Same shape as single-slot write: drop
    # cleared slots, set written slots.
    if coordinator.data is not None:
        for slot, prog in new_records:
            coordinator.data.programs[slot] = prog
        for slot in to_clear:
            coordinator.data.programs.pop(slot, None)

    connection.send_result(msg["id"], {
        "head_slot": head_slot,
        "written_slots": list(new_slot_range),
        "cleared_slots": sorted(to_clear),
    })


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/objects/list",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def _ws_list_objects(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return discovered objects so the frontend editor can populate
    object pickers (zone / unit / area / thermostat / button).

    Returns a flat dict mapping each kind to a list of
    ``{index, name}`` entries in slot order. Cached client-side after
    the first call — the topology doesn't change unless the user
    reloads the integration.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return
    data = coordinator.data
    if data is None:
        connection.send_result(msg["id"], {})
        return

    def _flatten(bucket) -> list[dict[str, Any]]:
        return [
            {"index": idx, "name": getattr(obj, "name", "") or f"slot {idx}"}
            for idx, obj in sorted(bucket.items())
        ]

    connection.send_result(msg["id"], {
        "zones": _flatten(data.zones),
        "units": _flatten(data.units),
        "areas": _flatten(data.areas),
        "thermostats": _flatten(data.thermostats),
        "buttons": _flatten(data.buttons),
    })


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/write",
        vol.Required("entry_id"): str,
        vol.Required("slot"): vol.All(int, vol.Range(min=1, max=1500)),
        vol.Required("program"): dict,
    }
)
@websocket_api.async_response
async def _ws_write_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Write an arbitrary Program record to ``slot``.

    The ``program`` payload is a JSON-friendly dict mirroring the
    :class:`omni_pca.programs.Program` dataclass — every field passed
    by name. Default 0 for fields the caller omits (matches the
    dataclass defaults). ``remark_id`` is optional / None.

    Frontend's edit form posts the whole struct on save; the slot is
    re-stamped to ``msg["slot"]`` in case the caller forgot. Saves
    update ``coordinator.data.programs[slot]`` immediately so the
    next list call shows the edit before the next poll catches up.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return
    try:
        validated = _PROGRAM_FIELD_SCHEMA(msg["program"])
    except vol.Invalid as err:
        connection.send_error(msg["id"], "invalid", f"bad program payload: {err}")
        return
    try:
        client = coordinator.client
    except RuntimeError as err:
        connection.send_error(msg["id"], "not_connected", str(err))
        return

    from omni_pca.programs import Program  # local — avoid cycle

    program = Program(slot=msg["slot"], **validated)
    try:
        await client.download_program(msg["slot"], program)
    except NotImplementedError as err:
        connection.send_error(msg["id"], "not_supported", str(err))
        return
    except Exception as err:
        connection.send_error(msg["id"], "write_failed", str(err))
        return
    if coordinator.data is not None:
        coordinator.data.programs[msg["slot"]] = program
    connection.send_result(
        msg["id"], {"slot": msg["slot"], "written": True},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/clear",
        vol.Required("entry_id"): str,
        vol.Required("slot"): vol.All(int, vol.Range(min=1, max=1500)),
    }
)
@websocket_api.async_response
async def _ws_clear_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Erase a program slot by writing an all-zero 14-byte body.

    Equivalent to "delete this program". v1 panels report
    ``not_supported`` because their wire protocol only allows bulk
    rewrites (which would clear everything).
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return
    try:
        client = coordinator.client
    except RuntimeError as err:
        connection.send_error(msg["id"], "not_connected", str(err))
        return
    try:
        await client.clear_program(msg["slot"])
    except NotImplementedError as err:
        connection.send_error(msg["id"], "not_supported", str(err))
        return
    except Exception as err:
        connection.send_error(msg["id"], "clear_failed", str(err))
        return
    # Drop the entry from the coordinator's in-memory view so subsequent
    # ``list`` calls reflect the deletion before the next poll catches up.
    if coordinator.data is not None:
        coordinator.data.programs.pop(msg["slot"], None)
    connection.send_result(msg["id"], {"slot": msg["slot"], "cleared": True})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/clone",
        vol.Required("entry_id"): str,
        vol.Required("source_slot"): vol.All(int, vol.Range(min=1, max=1500)),
        vol.Required("target_slot"): vol.All(int, vol.Range(min=1, max=1500)),
    }
)
@websocket_api.async_response
async def _ws_clone_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Copy ``source_slot``'s program into ``target_slot``.

    Useful for "I want a slightly different version of this program" —
    user clones into an empty slot, then (eventually, when the editor
    UI lands) tweaks the fields and saves.

    Refuses to clone when source and target are the same slot or when
    the source slot is empty / not defined.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return
    src = msg["source_slot"]
    dst = msg["target_slot"]
    if src == dst:
        connection.send_error(
            msg["id"], "invalid", "source and target slots must differ",
        )
        return
    programs = coordinator.data.programs if coordinator.data else {}
    source_program = programs.get(src)
    if source_program is None or source_program.is_empty():
        connection.send_error(
            msg["id"], "not_found", f"no program at source slot {src}",
        )
        return
    try:
        client = coordinator.client
    except RuntimeError as err:
        connection.send_error(msg["id"], "not_connected", str(err))
        return
    # The Program dataclass carries the slot field; re-stamp it for the
    # destination so the on-the-wire bytes are correctly addressed.
    from omni_pca.programs import Program  # local — avoid cycle
    cloned = Program(
        slot=dst,
        prog_type=source_program.prog_type,
        cond=source_program.cond,
        cond2=source_program.cond2,
        cmd=source_program.cmd,
        par=source_program.par,
        pr2=source_program.pr2,
        month=source_program.month,
        day=source_program.day,
        days=source_program.days,
        hour=source_program.hour,
        minute=source_program.minute,
        remark_id=source_program.remark_id,
    )
    try:
        await client.download_program(dst, cloned)
    except NotImplementedError as err:
        connection.send_error(msg["id"], "not_supported", str(err))
        return
    except Exception as err:
        connection.send_error(msg["id"], "clone_failed", str(err))
        return
    if coordinator.data is not None:
        coordinator.data.programs[dst] = cloned
    connection.send_result(
        msg["id"], {"source_slot": src, "target_slot": dst, "cloned": True},
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "omni_pca/programs/fire",
        vol.Required("entry_id"): str,
        vol.Required("slot"): vol.All(int, vol.Range(min=1, max=1500)),
    }
)
@websocket_api.async_response
async def _ws_fire_program(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Ask the panel to execute a program right now.

    Sends ``Command(EXECUTE_PROGRAM, parameter2=slot)`` via the
    coordinator's :class:`OmniClient`. The panel acks; any state
    changes the program triggers come back as ordinary push events.
    """
    coordinator = _coordinator_for_entry(hass, msg["entry_id"])
    if coordinator is None:
        connection.send_error(msg["id"], "not_found", "panel not configured")
        return
    try:
        client = coordinator.client
    except RuntimeError as err:
        connection.send_error(msg["id"], "not_connected", str(err))
        return
    try:
        await client.execute_command(Command.EXECUTE_PROGRAM, parameter2=msg["slot"])
    except Exception as err:
        connection.send_error(msg["id"], "fire_failed", str(err))
        return
    connection.send_result(msg["id"], {"slot": msg["slot"], "fired": True})


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------


@callback
def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Idempotently register the program-viewer websocket commands.

    Called from the integration's ``async_setup_entry``; safe to call
    once per HA boot. HA's ``websocket_api.async_register_command`` is
    itself idempotent so a stray double-call from reload paths is fine.
    """
    websocket_api.async_register_command(hass, _ws_list_programs)
    websocket_api.async_register_command(hass, _ws_get_program)
    websocket_api.async_register_command(hass, _ws_fire_program)
    websocket_api.async_register_command(hass, _ws_clear_program)
    websocket_api.async_register_command(hass, _ws_clone_program)
    websocket_api.async_register_command(hass, _ws_write_program)
    websocket_api.async_register_command(hass, _ws_chain_write)
    websocket_api.async_register_command(hass, _ws_list_objects)


# --------------------------------------------------------------------------
# Side-panel registration
# --------------------------------------------------------------------------


# Where the integration serves the bundled panel JS from. Phase C builds
# the actual ESM bundle and drops it at ``custom_components/omni_pca/
# www/panel.js`` — we register a static path so HA serves it at
# ``/api/omni_pca/panel.js``.
_PANEL_FRONTEND_URL: str = "omni-panel-programs"
_PANEL_WEBCOMPONENT: str = "omni-panel-programs"
_PANEL_JS_PATH: str = "/api/omni_pca/panel.js"


async def async_register_side_panel(hass: HomeAssistant) -> None:
    """Register the sidebar entry that hosts the program viewer.

    The bundled panel JS is served from the integration's ``www/``
    directory via a registered static path. Until Phase C ships the
    bundle, the panel registration still appears (HA shows a generic
    loader) so the wiring can be exercised end-to-end.
    """
    from pathlib import Path

    from homeassistant.components.frontend import (
        async_remove_panel,
    )
    from homeassistant.components.panel_custom import async_register_panel

    # Serve <integration>/www/panel.js at /api/omni_pca/panel.js.
    www_dir = Path(__file__).parent / "www"
    www_dir.mkdir(exist_ok=True)
    panel_js = www_dir / "panel.js"
    if not panel_js.exists():
        # Stub so the static path resolves even before Phase C builds the
        # real bundle. The stub renders a "panel coming soon" message so
        # users on dev installs see something useful rather than 404.
        panel_js.write_text(_STUB_PANEL_JS)
    await hass.http.async_register_static_paths(
        [_StaticPathConfig(_PANEL_JS_PATH, str(panel_js), False)]
    )

    # async_remove_panel before re-register so reload doesn't duplicate.
    try:
        async_remove_panel(hass, _PANEL_FRONTEND_URL)
    except Exception:
        pass
    await async_register_panel(
        hass,
        frontend_url_path=_PANEL_FRONTEND_URL,
        webcomponent_name=_PANEL_WEBCOMPONENT,
        sidebar_title="Omni Programs",
        sidebar_icon="mdi:script-text-outline",
        module_url=_PANEL_JS_PATH,
        embed_iframe=False,
        require_admin=False,
    )


_STUB_PANEL_JS: str = """\
// omni-pca side panel — stub until Phase C frontend lands.
class OmniPanelPrograms extends HTMLElement {
  set hass(hass) {
    if (!this._rendered) {
      this.innerHTML = `
        <style>
          :host, .root { display: block; padding: 24px; font-family: sans-serif; }
          h1 { font-size: 1.25rem; margin: 0 0 8px; }
          p  { color: #666; margin: 0; }
        </style>
        <div class="root">
          <h1>Omni Programs</h1>
          <p>Frontend bundle not yet installed.
             Phase C of the program viewer will populate this panel.</p>
        </div>`;
      this._rendered = true;
    }
  }
}
customElements.define('omni-panel-programs', OmniPanelPrograms);
"""


# Late import: HA's StaticPathConfig moved in 2024.7. The integration is
# pinned to a current HA release so this import works, but importing at
# module top would force the dep on tests that don't need the panel.
from homeassistant.components.http import StaticPathConfig as _StaticPathConfig  # noqa: E402
