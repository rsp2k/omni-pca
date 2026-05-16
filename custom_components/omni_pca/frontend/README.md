# Omni Programs side panel — frontend

Lit/TypeScript source for the HA side panel registered by
`websocket.py:async_register_side_panel`. The build output
(`../www/panel.js`) is committed so end-users don't need Node installed.

## Edit / rebuild

```bash
cd custom_components/omni_pca/frontend
npm install         # one-time
npm run build       # one-shot — drops a fresh ../www/panel.js
npm run watch       # rebuild on change (use during HA dev)
```

The build script (`build.mjs`) bundles the entry point + Lit + all
imports into a single ESM file at `../www/panel.js`. Source maps are
inlined in `--watch` mode and stripped in production builds. Output is
~34 KB minified.

## Layout

| File | Purpose |
|---|---|
| `src/omni-panel-programs.ts` | The custom-element entry point. Defines `<omni-panel-programs>` (matching the panel_custom registration). |
| `src/token-renderer.ts` | Token stream → Lit `TemplateResult`. Each TokenKind gets distinctive styling; REF tokens become buttons that dispatch a click. |
| `src/types.ts` | TS interfaces mirroring the Phase-B websocket wire shapes. Short keys (`k`/`t`/`ek`/`ei`/`s`) match `websocket.py:_tokens_to_json`. |

## Wire contract

The panel calls three websocket commands (all defined in
`../websocket.py`):

* `omni_pca/programs/list` — paginated, filterable summaries.
* `omni_pca/programs/get`  — full structured-English detail for one slot.
* `omni_pca/programs/fire` — sends `Command.EXECUTE_PROGRAM` over the wire.

The frontend doesn't subscribe to push events; live-state badges
refresh on a low-frequency poll (`REFRESH_MS = 5000`). That's a
deliberate scope choice — switching to per-entity event subscription
is a follow-up if the polling overhead becomes visible on huge installs.
