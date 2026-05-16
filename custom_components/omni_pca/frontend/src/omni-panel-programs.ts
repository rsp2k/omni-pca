// Side panel for browsing HAI Omni Panel programs. Custom-element name
// matches the websocket.py:_PANEL_WEBCOMPONENT registration.
//
// Layout: filter chips + search across the top, paginated program list
// in the main column, sliding detail panel on the right when a row is
// selected. Live-state badges refresh on a low-frequency poll
// (`state_changed` events would be more efficient but require
// per-entity subscription bookkeeping; the poll keeps Phase C scope
// honest — easy upgrade target later).

import { LitElement, html, css, PropertyValues, TemplateResult } from "lit";
import { customElement, property, state } from "lit/decorators.js";
import { renderTokens } from "./token-renderer.js";
import {
  Hass,
  ProgramDetail,
  ProgramListResponse,
  ProgramRow,
} from "./types.js";

const TRIGGER_TYPES = [
  "TIMED", "EVENT", "YEARLY", "WHEN", "AT", "EVERY", "REMARK",
] as const;

// How often the live-state overlay refreshes. Low enough that a panel
// fire shows up promptly, high enough that 330 programs × 4-byte deltas
// don't generate websocket noise.
const REFRESH_MS = 5000;


@customElement("omni-panel-programs")
export class OmniPanelPrograms extends LitElement {
  // -- HA-supplied properties -------------------------------------------

  @property({ attribute: false }) hass!: Hass;
  @property({ attribute: false }) narrow = false;

  // -- local state ------------------------------------------------------

  /** All omni_pca config entries discovered from hass.config.entries.
   *  Currently we just pick the first one — multi-panel installs would
   *  add a selector here. The websocket commands take an explicit
   *  entry_id so this is a UI concern, not a wire concern. */
  @state() private _entryId: string | null = null;

  @state() private _rows: ProgramRow[] = [];
  @state() private _total = 0;
  @state() private _filteredTotal = 0;
  @state() private _loading = false;
  @state() private _error: string | null = null;

  // Filters
  @state() private _activeTriggerTypes: Set<string> = new Set();
  @state() private _referenceFilter: string | null = null;
  @state() private _searchTerm: string = "";

  // Detail panel
  @state() private _selectedSlot: number | null = null;
  @state() private _detail: ProgramDetail | null = null;
  @state() private _detailLoading = false;
  @state() private _fireFeedback: string | null = null;

  private _refreshTimer: number | null = null;

  // -- lifecycle --------------------------------------------------------

  connectedCallback(): void {
    super.connectedCallback();
    this._discoverEntry();
    if (this._entryId) {
      this._loadList();
      this._startRefreshTimer();
    }
  }

  disconnectedCallback(): void {
    super.disconnectedCallback();
    this._stopRefreshTimer();
  }

  protected updated(changed: PropertyValues): void {
    if (changed.has("hass") && this._entryId === null) {
      this._discoverEntry();
      if (this._entryId) {
        this._loadList();
        this._startRefreshTimer();
      }
    }
  }

  // -- entry discovery --------------------------------------------------

  private _discoverEntry(): void {
    // hass.config.entries shape varies by HA version; most installs have
    // exactly one omni_pca panel, so we just probe a list endpoint to
    // find any entry_id our websocket accepts.
    if (!this.hass?.connection) return;
    void this._discoverViaList();
  }

  private async _discoverViaList(): Promise<void> {
    // Best-effort: walk known config entries via HA's standard
    // config_entries/get command. If that fails we surface a friendly
    // error pointing at integration setup.
    try {
      const entries = await this.hass.connection.sendMessagePromise<
        Array<{ entry_id: string; domain: string; title: string }>
      >({ type: "config_entries/get" });
      const ours = entries.filter((e) => e.domain === "omni_pca");
      if (ours.length === 0) {
        this._error = "No Omni panel configured. Add one via Settings → Devices & Services.";
        return;
      }
      // First entry wins for v1; multi-panel selector is a follow-up.
      this._entryId = ours[0].entry_id;
      this._error = null;
    } catch (err) {
      this._error = `Could not discover panels: ${err instanceof Error ? err.message : String(err)}`;
    }
  }

  // -- data loading -----------------------------------------------------

  private async _loadList(): Promise<void> {
    if (!this._entryId) return;
    this._loading = true;
    this._error = null;
    try {
      const msg: Record<string, unknown> = {
        type: "omni_pca/programs/list",
        entry_id: this._entryId,
      };
      if (this._activeTriggerTypes.size > 0) {
        msg.trigger_types = [...this._activeTriggerTypes];
      }
      if (this._referenceFilter) {
        msg.references_entity = this._referenceFilter;
      }
      if (this._searchTerm) {
        msg.search = this._searchTerm;
      }
      const result = await this.hass.connection.sendMessagePromise<ProgramListResponse>(msg);
      this._rows = result.programs;
      this._total = result.total;
      this._filteredTotal = result.filtered_total;
    } catch (err) {
      this._error = err instanceof Error ? err.message : String(err);
    } finally {
      this._loading = false;
    }
  }

  private async _loadDetail(slot: number): Promise<void> {
    if (!this._entryId) return;
    this._detailLoading = true;
    this._detail = null;
    try {
      this._detail = await this.hass.connection.sendMessagePromise({
        type: "omni_pca/programs/get",
        entry_id: this._entryId,
        slot,
      });
    } catch (err) {
      this._error = err instanceof Error ? err.message : String(err);
    } finally {
      this._detailLoading = false;
    }
  }

  private async _fireProgram(slot: number): Promise<void> {
    if (!this._entryId) return;
    this._fireFeedback = "firing…";
    try {
      await this.hass.connection.sendMessagePromise({
        type: "omni_pca/programs/fire",
        entry_id: this._entryId,
        slot,
      });
      this._fireFeedback = `fired slot ${slot}`;
      // Live-state will refresh on the next poll tick.
    } catch (err) {
      this._fireFeedback = `error: ${err instanceof Error ? err.message : err}`;
    }
    // Auto-clear feedback after a beat.
    setTimeout(() => { this._fireFeedback = null; }, 4000);
  }

  // -- refresh timer ----------------------------------------------------

  private _startRefreshTimer(): void {
    if (this._refreshTimer !== null) return;
    this._refreshTimer = window.setInterval(() => {
      void this._loadList();
      if (this._selectedSlot !== null) {
        void this._loadDetail(this._selectedSlot);
      }
    }, REFRESH_MS);
  }

  private _stopRefreshTimer(): void {
    if (this._refreshTimer !== null) {
      window.clearInterval(this._refreshTimer);
      this._refreshTimer = null;
    }
  }

  // -- filter handlers --------------------------------------------------

  private _toggleTriggerFilter(t: string): void {
    const next = new Set(this._activeTriggerTypes);
    if (next.has(t)) next.delete(t); else next.add(t);
    this._activeTriggerTypes = next;
    void this._loadList();
  }

  private _onSearchInput(e: Event): void {
    this._searchTerm = (e.target as HTMLInputElement).value;
    void this._loadList();
  }

  private _clearReferenceFilter(): void {
    this._referenceFilter = null;
    void this._loadList();
  }

  private _onRowClick(slot: number): void {
    this._selectedSlot = slot;
    void this._loadDetail(slot);
  }

  private _onRefClick(kind: string, id: number): void {
    // Click on any object reference in a token stream → filter the
    // list to programs that mention that entity. Clears the detail
    // panel since the new filter scope makes the old selection less
    // meaningful.
    this._referenceFilter = `${kind}:${id}`;
    this._selectedSlot = null;
    this._detail = null;
    void this._loadList();
  }

  private _closeDetail(): void {
    this._selectedSlot = null;
    this._detail = null;
  }

  // -- render -----------------------------------------------------------

  protected render(): TemplateResult {
    return html`
      <div class="header">
        <div class="title">
          <ha-icon icon="mdi:script-text-outline"></ha-icon>
          <span>Omni Programs</span>
          ${this._total > 0 ? html`
            <span class="count">
              ${this._filteredTotal === this._total
                ? `${this._total} programs`
                : `${this._filteredTotal} of ${this._total} shown`}
            </span>` : ""}
        </div>
      </div>
      ${this._error ? html`
        <div class="error">${this._error}</div>` : ""}
      ${this._renderFilters()}
      <div class="body" data-narrow=${this.narrow}>
        ${this._renderList()}
        ${this._selectedSlot !== null ? this._renderDetail() : ""}
      </div>
    `;
  }

  private _renderFilters(): TemplateResult {
    return html`
      <div class="filters">
        <input
          type="search"
          class="search"
          placeholder="search programs..."
          .value=${this._searchTerm}
          @input=${this._onSearchInput}
        />
        <div class="chips">
          ${TRIGGER_TYPES.map((t) => html`
            <button
              type="button"
              class="chip ${this._activeTriggerTypes.has(t) ? "active" : ""}"
              @click=${() => this._toggleTriggerFilter(t)}
            >${t}</button>
          `)}
        </div>
        ${this._referenceFilter ? html`
          <div class="ref-filter">
            <span>filtering on <strong>${this._referenceFilter}</strong></span>
            <button type="button" @click=${this._clearReferenceFilter}>clear</button>
          </div>` : ""}
      </div>
    `;
  }

  private _renderList(): TemplateResult {
    if (this._loading && this._rows.length === 0) {
      return html`<div class="loading">loading…</div>`;
    }
    if (this._rows.length === 0) {
      return html`<div class="empty">No programs match the current filters.</div>`;
    }
    return html`
      <div class="list">
        ${this._rows.map((row) => html`
          <div
            class="row ${this._selectedSlot === row.slot ? "selected" : ""}"
            @click=${() => this._onRowClick(row.slot)}
          >
            <div class="row-slot">#${row.slot}</div>
            <div class="row-summary">
              ${renderTokens(row.summary, (k, i) => this._onRefClick(k, i))}
            </div>
            <div class="row-meta">
              <span class="trigger-badge trigger-${row.trigger_type.toLowerCase()}">
                ${row.trigger_type}
              </span>
              ${row.condition_count > 0 ? html`
                <span class="meta-pill">${row.condition_count} cond</span>` : ""}
              ${row.action_count > 1 ? html`
                <span class="meta-pill">${row.action_count} actions</span>` : ""}
            </div>
          </div>
        `)}
      </div>
    `;
  }

  private _renderDetail(): TemplateResult {
    if (this._detailLoading) {
      return html`<aside class="detail"><div class="loading">loading…</div></aside>`;
    }
    if (this._detail === null) {
      return html`<aside class="detail"></aside>`;
    }
    const d = this._detail;
    return html`
      <aside class="detail">
        <header>
          <div>
            <span class="trigger-badge trigger-${d.trigger_type.toLowerCase()}">
              ${d.trigger_type}
            </span>
            <span class="slot">slot #${d.slot}</span>
          </div>
          <button type="button" class="close" @click=${this._closeDetail}>×</button>
        </header>
        <pre class="detail-body">${renderTokens(d.tokens, (k, i) => this._onRefClick(k, i))}</pre>
        <footer>
          <button
            type="button"
            class="fire"
            @click=${() => this._fireProgram(d.slot)}
          >▶ Fire now</button>
          ${this._fireFeedback ? html`
            <span class="fire-feedback">${this._fireFeedback}</span>` : ""}
        </footer>
        ${d.chain_slots && d.chain_slots.length > 1 ? html`
          <div class="chain-info">
            spans slots
            ${d.chain_slots.map((s, i) => html`
              ${i > 0 ? "→" : ""}#${s}`)}
          </div>` : ""}
      </aside>
    `;
  }

  // -- styles -----------------------------------------------------------

  static styles = css`
    :host {
      display: block;
      min-height: 100vh;
      background: var(--primary-background-color, #fafafa);
      color: var(--primary-text-color, #000);
      font-family: var(--paper-font-body1_-_font-family, sans-serif);
    }
    .header {
      display: flex; align-items: center;
      padding: 16px 20px;
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
    }
    .header .title { display: flex; align-items: center; gap: 10px; font-size: 1.2rem; }
    .header .count {
      margin-left: 12px;
      font-size: 0.85rem; opacity: 0.85; font-weight: normal;
    }

    .error {
      margin: 12px 16px;
      padding: 10px 14px;
      background: var(--error-color, #db4437);
      color: white;
      border-radius: 4px;
    }

    .filters {
      padding: 12px 16px 8px;
      border-bottom: 1px solid var(--divider-color, #ddd);
    }
    .search {
      width: 100%;
      padding: 8px 10px;
      font-size: 0.95rem;
      border: 1px solid var(--divider-color, #ccc);
      border-radius: 4px;
      background: var(--card-background-color, #fff);
      color: inherit;
      box-sizing: border-box;
    }
    .chips {
      display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px;
    }
    .chip {
      border: 1px solid var(--divider-color, #ccc);
      background: var(--card-background-color, #fff);
      color: var(--secondary-text-color, #555);
      padding: 4px 10px;
      border-radius: 12px;
      font-size: 0.78rem;
      cursor: pointer;
      font-family: inherit;
    }
    .chip:hover { background: var(--secondary-background-color, #eee); }
    .chip.active {
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
      border-color: transparent;
    }
    .ref-filter {
      margin-top: 8px;
      font-size: 0.85rem;
      color: var(--secondary-text-color, #555);
      display: flex; align-items: center; gap: 8px;
    }
    .ref-filter button {
      border: 1px solid var(--divider-color, #ccc);
      background: transparent; color: inherit;
      padding: 2px 8px; border-radius: 8px;
      font-size: 0.75rem; cursor: pointer;
    }

    .body {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0;
    }
    .body[data-narrow="false"] { grid-template-columns: 1fr 380px; }

    .list {
      max-height: calc(100vh - 200px);
      overflow-y: auto;
    }
    .row {
      display: grid;
      grid-template-columns: 60px 1fr auto;
      align-items: start;
      gap: 12px;
      padding: 10px 16px;
      border-bottom: 1px solid var(--divider-color, #eee);
      cursor: pointer;
    }
    .row:hover { background: var(--secondary-background-color, #f5f5f5); }
    .row.selected { background: var(--state-active-color, #e3f2fd); }
    .row-slot {
      font-family: var(--code-font-family, monospace);
      font-size: 0.78rem;
      color: var(--secondary-text-color, #888);
      padding-top: 2px;
    }
    .row-summary {
      font-size: 0.92rem;
      line-height: 1.45;
    }
    .row-meta {
      display: flex; flex-direction: column; align-items: flex-end; gap: 4px;
    }

    /* trigger-type badges */
    .trigger-badge {
      font-size: 0.7rem;
      font-weight: 600;
      letter-spacing: 0.5px;
      padding: 2px 6px;
      border-radius: 3px;
      text-transform: uppercase;
    }
    .trigger-timed { background: #e3f2fd; color: #1565c0; }
    .trigger-event { background: #fff3e0; color: #e65100; }
    .trigger-yearly { background: #f3e5f5; color: #6a1b9a; }
    .trigger-when { background: #e8f5e9; color: #2e7d32; }
    .trigger-at { background: #e3f2fd; color: #1565c0; }
    .trigger-every { background: #fce4ec; color: #ad1457; }
    .trigger-remark { background: #f5f5f5; color: #616161; }

    .meta-pill {
      font-size: 0.7rem;
      color: var(--secondary-text-color, #888);
      background: var(--secondary-background-color, #eee);
      padding: 1px 6px;
      border-radius: 8px;
    }

    /* token-renderer styles */
    .row-summary, .detail-body {
      font-family: var(--paper-font-body1_-_font-family, system-ui, sans-serif);
    }
    .keyword { font-weight: 600; color: var(--primary-color, #1565c0); }
    .operator { color: var(--secondary-text-color, #666); font-style: italic; }
    .value { font-family: var(--code-font-family, monospace); color: var(--accent-color, #ff6f00); }
    .ref {
      display: inline-flex; align-items: baseline; gap: 4px;
      border: none; background: transparent; padding: 0 2px;
      cursor: pointer; font: inherit; color: inherit;
      border-bottom: 1px dotted var(--secondary-text-color, #999);
    }
    .ref:hover { background: var(--secondary-background-color, #eee); }
    .ref-name { font-weight: 500; }
    .ref-state {
      font-size: 0.72rem;
      padding: 1px 5px;
      border-radius: 3px;
      background: var(--secondary-background-color, #eee);
      color: var(--secondary-text-color, #666);
      vertical-align: 1px;
    }
    .ref-zone .ref-name { color: var(--info-color, #0288d1); }
    .ref-unit .ref-name { color: var(--warning-color, #f57c00); }
    .ref-area .ref-name { color: var(--success-color, #388e3c); }
    .ref-thermostat .ref-name { color: var(--accent-color, #c2185b); }
    .ref-button .ref-name { color: var(--state-light-color, #7e57c2); }

    .indent { display: inline-block; width: 1.5em; }

    /* detail panel */
    .detail {
      border-left: 1px solid var(--divider-color, #ddd);
      padding: 16px;
      max-height: calc(100vh - 200px);
      overflow-y: auto;
      box-sizing: border-box;
    }
    .body[data-narrow="true"] .detail {
      border-left: none;
      border-top: 1px solid var(--divider-color, #ddd);
    }
    .detail header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 12px;
    }
    .detail header .slot {
      margin-left: 8px;
      font-family: var(--code-font-family, monospace);
      font-size: 0.85rem;
      color: var(--secondary-text-color, #888);
    }
    .detail .close {
      background: transparent; border: none;
      font-size: 1.4rem; cursor: pointer;
      color: var(--secondary-text-color, #888);
    }
    .detail-body {
      font-size: 0.95rem;
      line-height: 1.6;
      white-space: pre-wrap;
      word-wrap: break-word;
      background: var(--card-background-color, #fff);
      padding: 12px;
      border-radius: 4px;
      border: 1px solid var(--divider-color, #eee);
      margin: 0;
    }
    .detail footer {
      display: flex; align-items: center; gap: 12px; margin-top: 14px;
    }
    .fire {
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
      border: none;
      padding: 8px 16px;
      font-size: 0.92rem;
      border-radius: 4px;
      cursor: pointer;
    }
    .fire:hover { filter: brightness(0.9); }
    .fire-feedback {
      font-size: 0.85rem; color: var(--secondary-text-color, #666);
    }
    .chain-info {
      margin-top: 12px;
      font-size: 0.8rem;
      color: var(--secondary-text-color, #888);
    }

    .loading, .empty {
      padding: 40px 20px;
      text-align: center;
      color: var(--secondary-text-color, #888);
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "omni-panel-programs": OmniPanelPrograms;
  }
}
