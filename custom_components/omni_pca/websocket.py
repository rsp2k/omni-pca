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
        })
        return

    tokens = renderer.render_program(target)
    connection.send_result(msg["id"], {
        "slot": msg["slot"],
        "kind": "compact",
        "trigger_type": _classify_trigger(target),
        "tokens": _tokens_to_json(tokens),
        "references": _extract_references(tokens),
    })


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
