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
  COMMAND_OPTIONS,
  CommandOption,
  DAY_BITS,
  DecodedEvent,
  EventCategory,
  FIXED_EVENTS,
  Hass,
  MONTH_NAMES,
  NamedObject,
  ObjectListResponse,
  PROGRAM_TYPE_EVENT,
  PROGRAM_TYPE_TIMED,
  PROGRAM_TYPE_YEARLY,
  ProgramDetail,
  ProgramFields,
  ProgramListResponse,
  ProgramRow,
  commandOptionFor,
  decodeEventId,
  encodeEventId,
  eventIdFromFields,
  packEventIdIntoFields,
} from "./types.js";

// Which compact-form trigger types the editor knows how to render.
// REMARK is intentionally excluded (it's a text annotation, not a
// runnable program). Clausal types (WHEN/AT/EVERY) are kind="chain"
// not "compact" so they're filtered out earlier in _beginEdit.
const EDITABLE_PROG_TYPES = new Set(["TIMED", "EVENT", "YEARLY"]);

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
  @state() private _writeFeedback: string | null = null;
  @state() private _cloneTargetSlot: string = "";
  @state() private _showCloneInput: boolean = false;
  @state() private _confirmingClear: boolean = false;

  // Edit mode: when non-null, the detail panel renders the form
  // instead of the structured-English read-only view. The draft is a
  // mutable working copy of the program; Save sends it via the
  // omni_pca/programs/write websocket, Cancel discards.
  @state() private _editingDraft: ProgramFields | null = null;
  @state() private _objects: ObjectListResponse | null = null;

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

  private async _clearProgram(slot: number): Promise<void> {
    if (!this._entryId) return;
    this._writeFeedback = "clearing…";
    try {
      await this.hass.connection.sendMessagePromise({
        type: "omni_pca/programs/clear",
        entry_id: this._entryId,
        slot,
      });
      this._writeFeedback = `cleared slot ${slot}`;
      this._confirmingClear = false;
      // Refresh the list + close the detail panel; the slot is gone.
      this._selectedSlot = null;
      this._detail = null;
      await this._loadList();
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this._writeFeedback = `error: ${message}`;
    }
    setTimeout(() => { this._writeFeedback = null; }, 4000);
  }

  private async _cloneProgram(sourceSlot: number): Promise<void> {
    if (!this._entryId) return;
    const targetRaw = this._cloneTargetSlot.trim();
    const target = parseInt(targetRaw, 10);
    if (!Number.isFinite(target) || target < 1 || target > 1500) {
      this._writeFeedback = "target slot must be 1..1500";
      setTimeout(() => { this._writeFeedback = null; }, 4000);
      return;
    }
    if (target === sourceSlot) {
      this._writeFeedback = "target must differ from source";
      setTimeout(() => { this._writeFeedback = null; }, 4000);
      return;
    }
    this._writeFeedback = "cloning…";
    try {
      await this.hass.connection.sendMessagePromise({
        type: "omni_pca/programs/clone",
        entry_id: this._entryId,
        source_slot: sourceSlot,
        target_slot: target,
      });
      this._writeFeedback = `cloned to slot ${target}`;
      this._showCloneInput = false;
      this._cloneTargetSlot = "";
      // Navigate to the new clone so the user sees the result.
      this._selectedSlot = target;
      await this._loadList();
      await this._loadDetail(target);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      this._writeFeedback = `error: ${message}`;
    }
    setTimeout(() => { this._writeFeedback = null; }, 4000);
  }

  private _onCloneTargetInput(e: Event): void {
    this._cloneTargetSlot = (e.target as HTMLInputElement).value;
  }

  // ---- editor -------------------------------------------------------

  private async _ensureObjectsLoaded(): Promise<void> {
    if (this._objects !== null || !this._entryId) return;
    try {
      this._objects = await this.hass.connection.sendMessagePromise({
        type: "omni_pca/objects/list",
        entry_id: this._entryId,
      });
    } catch (err) {
      // Picker dropdowns will fall back to "Slot N" labels — not fatal.
      const msg = err instanceof Error ? err.message : String(err);
      console.warn("omni_pca: objects/list failed", msg);
    }
  }

  private async _beginEdit(): Promise<void> {
    if (!this._detail || this._detail.kind !== "compact") return;
    // The frontend supports editing compact-form TIMED / EVENT / YEARLY
    // programs. Other compact types (REMARK) and clausal chains remain
    // read-only — the editor pathway returns early without seeding a
    // draft so the read-only view stays visible.
    if (!EDITABLE_PROG_TYPES.has(this._detail.trigger_type)) return;
    await this._ensureObjectsLoaded();
    if (!this._entryId) return;
    // The detail response now carries raw fields directly. If they're
    // missing (panel returned only tokens) we fall back to sensible
    // defaults so the form at least opens — better than a hard error.
    const fields = this._detail.fields ?? this._defaultFieldsForType(
      this._detail.trigger_type,
    );
    if (fields === null) return;
    this._editingDraft = { ...fields };
    this._stopRefreshTimer();
  }

  private _defaultFieldsForType(triggerType: string): ProgramFields | null {
    const firstUnit = this._objects?.units?.[0]?.index ?? 1;
    if (triggerType === "TIMED") {
      return {
        prog_type: PROGRAM_TYPE_TIMED,
        cmd: 1, par: 0, pr2: firstUnit,
        hour: 6, minute: 0,
        days: 0x02 | 0x04 | 0x08 | 0x10 | 0x20,  // Mon-Fri
        cond: 0, cond2: 0, month: 0, day: 0,
      };
    }
    if (triggerType === "EVENT") {
      const firstButton = this._objects?.buttons?.[0]?.index ?? 1;
      return {
        prog_type: PROGRAM_TYPE_EVENT,
        cmd: 1, par: 0, pr2: firstUnit,
        // Default to a button-press event; month+day pack the event_id.
        month: 0, day: firstButton & 0xFF,
        hour: 0, minute: 0, days: 0,
        cond: 0, cond2: 0,
      };
    }
    if (triggerType === "YEARLY") {
      return {
        prog_type: PROGRAM_TYPE_YEARLY,
        cmd: 1, par: 0, pr2: firstUnit,
        month: 1, day: 1, hour: 0, minute: 0,
        days: 0, cond: 0, cond2: 0,
      };
    }
    return null;
  }

  private async _saveDraft(): Promise<void> {
    if (!this._editingDraft || !this._detail || !this._entryId) return;
    this._writeFeedback = "saving…";
    try {
      await this.hass.connection.sendMessagePromise({
        type: "omni_pca/programs/write",
        entry_id: this._entryId,
        slot: this._detail.slot,
        program: this._editingDraft,
      });
      this._writeFeedback = `saved slot ${this._detail.slot}`;
      this._editingDraft = null;
      this._startRefreshTimer();
      // Refresh both panels so the new values land in the UI.
      await this._loadList();
      await this._loadDetail(this._detail.slot);
    } catch (err) {
      const m = err instanceof Error ? err.message : String(err);
      this._writeFeedback = `error: ${m}`;
    }
    setTimeout(() => { this._writeFeedback = null; }, 4000);
  }

  private _cancelEdit(): void {
    this._editingDraft = null;
    this._startRefreshTimer();
  }

  private _patchDraft(patch: Partial<ProgramFields>): void {
    if (!this._editingDraft) return;
    this._editingDraft = { ...this._editingDraft, ...patch };
  }

  private _toggleDayBit(bit: number): void {
    if (!this._editingDraft) return;
    const current = this._editingDraft.days ?? 0;
    const next = current ^ bit;
    this._patchDraft({ days: next });
  }

  private _onCommandChange(e: Event): void {
    const value = parseInt((e.target as HTMLSelectElement).value, 10);
    if (!Number.isFinite(value)) return;
    // Picking a new command often makes the existing pr2 invalid for
    // its new object kind — reset pr2 to the first object of the new
    // kind so the form stays consistent.
    const opt = commandOptionFor(value);
    let newPr2 = this._editingDraft?.pr2 ?? 0;
    if (opt?.ref_kind && this._objects) {
      const bucket = this._pickBucket(opt.ref_kind);
      if (bucket && bucket.length > 0 &&
          !bucket.some((o) => o.index === newPr2)) {
        newPr2 = bucket[0].index;
      }
    } else if (!opt?.ref_kind) {
      newPr2 = 0;
    }
    this._patchDraft({ cmd: value, pr2: newPr2 });
  }

  private _pickBucket(kind: string): NamedObject[] | null {
    if (!this._objects) return null;
    switch (kind) {
      case "zone":   return this._objects.zones;
      case "unit":   return this._objects.units;
      case "area":   return this._objects.areas;
      case "button": return this._objects.buttons;
      default: return null;
    }
  }

  private _onObjectChange(e: Event): void {
    const value = parseInt((e.target as HTMLSelectElement).value, 10);
    if (Number.isFinite(value)) this._patchDraft({ pr2: value });
  }

  private _onHourChange(e: Event): void {
    const value = parseInt((e.target as HTMLInputElement).value, 10);
    if (Number.isFinite(value) && value >= 0 && value <= 23) {
      this._patchDraft({ hour: value });
    }
  }

  private _onMinuteChange(e: Event): void {
    const value = parseInt((e.target as HTMLInputElement).value, 10);
    if (Number.isFinite(value) && value >= 0 && value <= 59) {
      this._patchDraft({ minute: value });
    }
  }

  private _onParChange(e: Event): void {
    const value = parseInt((e.target as HTMLInputElement).value, 10);
    if (Number.isFinite(value) && value >= 0 && value <= 255) {
      this._patchDraft({ par: value });
    }
  }

  // ---- YEARLY handlers (month / day) ---------------------------------

  private _onMonthChange(e: Event): void {
    const value = parseInt((e.target as HTMLSelectElement).value, 10);
    if (Number.isFinite(value) && value >= 1 && value <= 12) {
      this._patchDraft({ month: value });
    }
  }

  private _onDayChange(e: Event): void {
    const value = parseInt((e.target as HTMLInputElement).value, 10);
    if (Number.isFinite(value) && value >= 1 && value <= 31) {
      this._patchDraft({ day: value });
    }
  }

  // ---- EVENT handlers (event-id builder) -----------------------------
  //
  // The event_id is packed into the program's month/day bytes
  // (eventId >> 8 = month, eventId & 0xFF = day) — that's the wire
  // encoding for EVENT records. The UI works in terms of "category +
  // sub-fields" and re-encodes on every change.

  private _patchEvent(decoded: DecodedEvent): void {
    if (!this._editingDraft) return;
    const eventId = encodeEventId(decoded);
    this._editingDraft = packEventIdIntoFields(this._editingDraft, eventId);
  }

  private _onEventCategoryChange(e: Event): void {
    const cat = (e.target as HTMLSelectElement).value as EventCategory;
    // Switching category — seed sensible defaults for the new category
    // so the sub-fields below have valid initial values.
    if (cat === "button") {
      const firstButton = this._objects?.buttons?.[0]?.index ?? 1;
      this._patchEvent({ category: "button", button: firstButton });
    } else if (cat === "zone") {
      const firstZone = this._objects?.zones?.[0]?.index ?? 1;
      this._patchEvent({ category: "zone", zone: firstZone, zoneState: 1 });
    } else if (cat === "unit") {
      const firstUnit = this._objects?.units?.[0]?.index ?? 1;
      this._patchEvent({ category: "unit", unit: firstUnit, unitOn: true });
    } else if (cat === "fixed") {
      this._patchEvent({ category: "fixed", fixedId: 772 });  // AC lost
    }
    // "raw" isn't user-selectable from the dropdown — only appears when
    // an existing event ID doesn't match a known pattern.
  }

  private _onEventButtonChange(e: Event): void {
    const button = parseInt((e.target as HTMLSelectElement).value, 10);
    if (Number.isFinite(button)) {
      this._patchEvent({ category: "button", button });
    }
  }

  private _onEventZoneChange(e: Event): void {
    if (!this._editingDraft) return;
    const zone = parseInt((e.target as HTMLSelectElement).value, 10);
    if (!Number.isFinite(zone)) return;
    const existing = decodeEventId(eventIdFromFields(this._editingDraft));
    this._patchEvent({
      category: "zone",
      zone,
      zoneState: existing.zoneState ?? 1,
    });
  }

  private _onEventZoneStateChange(e: Event): void {
    if (!this._editingDraft) return;
    const state = parseInt((e.target as HTMLSelectElement).value, 10);
    if (!Number.isFinite(state)) return;
    const existing = decodeEventId(eventIdFromFields(this._editingDraft));
    this._patchEvent({
      category: "zone",
      zone: existing.zone ?? 1,
      zoneState: state,
    });
  }

  private _onEventUnitChange(e: Event): void {
    if (!this._editingDraft) return;
    const unit = parseInt((e.target as HTMLSelectElement).value, 10);
    if (!Number.isFinite(unit)) return;
    const existing = decodeEventId(eventIdFromFields(this._editingDraft));
    this._patchEvent({
      category: "unit",
      unit,
      unitOn: existing.unitOn ?? true,
    });
  }

  private _onEventUnitOnChange(e: Event): void {
    if (!this._editingDraft) return;
    const on = (e.target as HTMLSelectElement).value === "1";
    const existing = decodeEventId(eventIdFromFields(this._editingDraft));
    this._patchEvent({
      category: "unit",
      unit: existing.unit ?? 1,
      unitOn: on,
    });
  }

  private _onEventFixedChange(e: Event): void {
    const id = parseInt((e.target as HTMLSelectElement).value, 10);
    if (Number.isFinite(id)) {
      this._patchEvent({ category: "fixed", fixedId: id });
    }
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
    if (this._editingDraft !== null) {
      return this._renderEditor(d);
    }
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
          ${d.kind === "compact" && EDITABLE_PROG_TYPES.has(d.trigger_type) ? html`
            <button
              type="button"
              class="secondary"
              @click=${this._beginEdit}
            >Edit</button>` : ""}
          <button
            type="button"
            class="secondary"
            @click=${() => {
              this._showCloneInput = !this._showCloneInput;
              this._confirmingClear = false;
            }}
          >Clone…</button>
          <button
            type="button"
            class="danger"
            @click=${() => {
              this._confirmingClear = !this._confirmingClear;
              this._showCloneInput = false;
            }}
          >Clear</button>
          ${this._fireFeedback ? html`
            <span class="fire-feedback">${this._fireFeedback}</span>` : ""}
          ${this._writeFeedback ? html`
            <span class="fire-feedback">${this._writeFeedback}</span>` : ""}
        </footer>
        ${this._showCloneInput ? html`
          <div class="action-row">
            <label>Clone slot ${d.slot} → target slot:
              <input
                type="number"
                min="1"
                max="1500"
                .value=${this._cloneTargetSlot}
                @input=${this._onCloneTargetInput}
                @keydown=${(e: KeyboardEvent) => {
                  if (e.key === "Enter") this._cloneProgram(d.slot);
                }}
              />
            </label>
            <button
              type="button"
              class="primary"
              @click=${() => this._cloneProgram(d.slot)}
            >Clone</button>
            <button
              type="button"
              @click=${() => { this._showCloneInput = false; }}
            >Cancel</button>
          </div>` : ""}
        ${this._confirmingClear ? html`
          <div class="action-row danger-row">
            <span>
              <strong>Clear slot ${d.slot}?</strong>
              This deletes the program from the panel.
            </span>
            <button
              type="button"
              class="danger"
              @click=${() => this._clearProgram(d.slot)}
            >Yes, clear</button>
            <button
              type="button"
              @click=${() => { this._confirmingClear = false; }}
            >Cancel</button>
          </div>` : ""}
        ${d.chain_slots && d.chain_slots.length > 1 ? html`
          <div class="chain-info">
            spans slots
            ${d.chain_slots.map((s, i) => html`
              ${i > 0 ? "→" : ""}#${s}`)}
          </div>` : ""}
      </aside>
    `;
  }

  private _renderEditor(d: ProgramDetail): TemplateResult {
    const draft = this._editingDraft!;
    const triggerLabel = d.trigger_type;
    return html`
      <aside class="detail editor">
        <header>
          <div>
            <span class="trigger-badge trigger-${triggerLabel.toLowerCase()}">
              EDIT • ${triggerLabel}
            </span>
            <span class="slot">slot #${d.slot}</span>
          </div>
          <button type="button" class="close" @click=${this._cancelEdit}>×</button>
        </header>

        <div class="editor-body">
          ${this._renderTriggerSection(draft)}
          ${this._renderActionSection(draft)}
          ${draft.cond || draft.cond2 ? html`
            <div class="conditions-readonly">
              <strong>Inline conditions:</strong>
              this program carries up to two inline AND-IF conditions on
              the source record. They're preserved on save but editing
              condition fields is not yet supported.
            </div>` : ""}
        </div>

        <footer>
          <button type="button" class="primary" @click=${this._saveDraft}>
            Save
          </button>
          <button type="button" class="secondary" @click=${this._cancelEdit}>
            Cancel
          </button>
          ${this._writeFeedback ? html`
            <span class="fire-feedback">${this._writeFeedback}</span>` : ""}
        </footer>
      </aside>
    `;
  }

  private _renderTriggerSection(draft: ProgramFields): TemplateResult {
    switch (draft.prog_type) {
      case PROGRAM_TYPE_TIMED:
        return this._renderTimedTrigger(draft);
      case PROGRAM_TYPE_EVENT:
        return this._renderEventTrigger(draft);
      case PROGRAM_TYPE_YEARLY:
        return this._renderYearlyTrigger(draft);
      default:
        return html`<div class="conditions-readonly">
          Editing program type ${draft.prog_type} is not supported.
        </div>`;
    }
  }

  private _renderTimedTrigger(draft: ProgramFields): TemplateResult {
    return html`
      <fieldset>
        <legend>Time</legend>
        <div class="row">
          <label>
            Hour
            <input
              type="number" min="0" max="23"
              .value=${String(draft.hour ?? 0)}
              @input=${this._onHourChange}
            />
          </label>
          <span class="time-colon">:</span>
          <label>
            Minute
            <input
              type="number" min="0" max="59" step="1"
              .value=${String(draft.minute ?? 0)}
              @input=${this._onMinuteChange}
            />
          </label>
        </div>
      </fieldset>
      <fieldset>
        <legend>Days</legend>
        <div class="days-row">
          ${DAY_BITS.map((d) => {
            const active = ((draft.days ?? 0) & d.bit) !== 0;
            return html`
              <button
                type="button"
                class="day-toggle ${active ? "active" : ""}"
                @click=${() => this._toggleDayBit(d.bit)}
              >${d.label}</button>
            `;
          })}
        </div>
      </fieldset>
    `;
  }

  private _renderEventTrigger(draft: ProgramFields): TemplateResult {
    const eventId = eventIdFromFields(draft);
    const decoded = decodeEventId(eventId);
    return html`
      <fieldset>
        <legend>Trigger event</legend>
        <label class="block">
          Category
          <select @change=${this._onEventCategoryChange}>
            <option value="button"
                    ?selected=${decoded.category === "button"}>
              Button press
            </option>
            <option value="zone"
                    ?selected=${decoded.category === "zone"}>
              Zone state change
            </option>
            <option value="unit"
                    ?selected=${decoded.category === "unit"}>
              Unit state change
            </option>
            <option value="fixed"
                    ?selected=${decoded.category === "fixed"}>
              Fixed event (phone / AC)
            </option>
            ${decoded.category === "raw" ? html`
              <option value="raw" selected>
                Raw 0x${eventId.toString(16).padStart(4, "0")}
              </option>` : ""}
          </select>
        </label>
        ${this._renderEventCategoryFields(decoded)}
      </fieldset>
    `;
  }

  private _renderEventCategoryFields(decoded: DecodedEvent): TemplateResult {
    if (decoded.category === "button") {
      return html`
        <label class="block">
          Button
          <select @change=${this._onEventButtonChange}>
            ${(this._objects?.buttons ?? []).map((b) => html`
              <option .value=${String(b.index)}
                      ?selected=${b.index === decoded.button}>
                #${b.index} ${b.name}
              </option>
            `)}
          </select>
        </label>`;
    }
    if (decoded.category === "zone") {
      return html`
        <label class="block">
          Zone
          <select @change=${this._onEventZoneChange}>
            ${(this._objects?.zones ?? []).map((z) => html`
              <option .value=${String(z.index)}
                      ?selected=${z.index === decoded.zone}>
                #${z.index} ${z.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Becomes
          <select @change=${this._onEventZoneStateChange}>
            <option value="0" ?selected=${decoded.zoneState === 0}>secure</option>
            <option value="1" ?selected=${decoded.zoneState === 1}>not ready</option>
            <option value="2" ?selected=${decoded.zoneState === 2}>trouble</option>
            <option value="3" ?selected=${decoded.zoneState === 3}>tamper</option>
          </select>
        </label>`;
    }
    if (decoded.category === "unit") {
      return html`
        <label class="block">
          Unit
          <select @change=${this._onEventUnitChange}>
            ${(this._objects?.units ?? []).map((u) => html`
              <option .value=${String(u.index)}
                      ?selected=${u.index === decoded.unit}>
                #${u.index} ${u.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Turns
          <select @change=${this._onEventUnitOnChange}>
            <option value="1" ?selected=${decoded.unitOn === true}>ON</option>
            <option value="0" ?selected=${decoded.unitOn === false}>OFF</option>
          </select>
        </label>`;
    }
    if (decoded.category === "fixed") {
      return html`
        <label class="block">
          Event
          <select @change=${this._onEventFixedChange}>
            ${FIXED_EVENTS.map((f) => html`
              <option .value=${String(f.id)}
                      ?selected=${f.id === decoded.fixedId}>
                ${f.label}
              </option>
            `)}
          </select>
        </label>`;
    }
    // raw — render as informational; the user picked another category
    // from the dropdown if they want to change it.
    return html`
      <div class="conditions-readonly">
        Unrecognised event ID. Switch category above to redefine.
      </div>`;
  }

  private _renderYearlyTrigger(draft: ProgramFields): TemplateResult {
    return html`
      <fieldset>
        <legend>Date</legend>
        <div class="row">
          <label>
            Month
            <select @change=${this._onMonthChange}>
              ${MONTH_NAMES.map((name, i) => html`
                <option .value=${String(i + 1)}
                        ?selected=${(draft.month ?? 1) === i + 1}>
                  ${name} (${i + 1})
                </option>
              `)}
            </select>
          </label>
          <label>
            Day
            <input
              type="number" min="1" max="31"
              .value=${String(draft.day ?? 1)}
              @input=${this._onDayChange}
            />
          </label>
        </div>
      </fieldset>
      <fieldset>
        <legend>Time of day</legend>
        <div class="row">
          <label>
            Hour
            <input
              type="number" min="0" max="23"
              .value=${String(draft.hour ?? 0)}
              @input=${this._onHourChange}
            />
          </label>
          <span class="time-colon">:</span>
          <label>
            Minute
            <input
              type="number" min="0" max="59"
              .value=${String(draft.minute ?? 0)}
              @input=${this._onMinuteChange}
            />
          </label>
        </div>
      </fieldset>
    `;
  }

  private _renderActionSection(draft: ProgramFields): TemplateResult {
    const cmdOpt: CommandOption | undefined = commandOptionFor(draft.cmd ?? 0);
    const objectBucket = cmdOpt?.ref_kind ? this._pickBucket(cmdOpt.ref_kind) : null;
    const showsLevelPercent = (draft.cmd === 9);  // UNIT_LEVEL
    return html`
      <fieldset>
        <legend>Action</legend>
        <label class="block">
          Command
          <select @change=${this._onCommandChange}>
            ${COMMAND_OPTIONS.map((c) => html`
              <option .value=${String(c.value)}
                      ?selected=${c.value === draft.cmd}>
                ${c.label}
              </option>
            `)}
          </select>
        </label>
        ${cmdOpt?.ref_kind ? html`
          <label class="block">
            ${cmdOpt.ref_kind[0].toUpperCase() + cmdOpt.ref_kind.slice(1)}
            <select @change=${this._onObjectChange}>
              ${(objectBucket ?? []).map((o) => html`
                <option .value=${String(o.index)}
                        ?selected=${o.index === draft.pr2}>
                  #${o.index} ${o.name}
                </option>
              `)}
            </select>
          </label>` : ""}
        ${showsLevelPercent ? html`
          <label class="block">
            Level (0..100)
            <input
              type="number" min="0" max="100"
              .value=${String(draft.par ?? 0)}
              @input=${this._onParChange}
            />
          </label>` : ""}
      </fieldset>
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
    .fire, .primary, .secondary, .danger {
      border: none;
      padding: 8px 16px;
      font-size: 0.92rem;
      border-radius: 4px;
      cursor: pointer;
      font-family: inherit;
    }
    .fire, .primary {
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
    }
    .secondary {
      background: var(--secondary-background-color, #eee);
      color: var(--primary-text-color, #000);
    }
    .danger {
      background: transparent;
      color: var(--error-color, #db4437);
      border: 1px solid var(--error-color, #db4437);
    }
    .fire:hover, .primary:hover, .secondary:hover, .danger:hover {
      filter: brightness(0.9);
    }
    .action-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 12px;
      padding: 10px;
      background: var(--secondary-background-color, #f5f5f5);
      border-radius: 4px;
      font-size: 0.88rem;
    }
    .action-row.danger-row {
      background: var(--error-color, #db4437);
      color: white;
    }
    .action-row input[type="number"] {
      width: 70px;
      padding: 4px 6px;
      font-size: 0.9rem;
      border: 1px solid var(--divider-color, #ccc);
      border-radius: 3px;
      margin-left: 6px;
    }
    .action-row button {
      padding: 4px 12px;
      font-size: 0.85rem;
    }
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

    /* editor */
    .editor-body { display: flex; flex-direction: column; gap: 12px; }
    .editor fieldset {
      border: 1px solid var(--divider-color, #ddd);
      border-radius: 4px;
      padding: 10px 12px;
      margin: 0;
    }
    .editor legend {
      padding: 0 6px;
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--secondary-text-color, #777);
    }
    .editor .row {
      display: flex; align-items: center; gap: 8px;
    }
    .editor label.block {
      display: flex; flex-direction: column;
      gap: 4px;
      font-size: 0.85rem;
      color: var(--secondary-text-color, #555);
      margin-bottom: 8px;
    }
    .editor label.block:last-child { margin-bottom: 0; }
    .editor input[type="number"], .editor select {
      padding: 6px 8px;
      font-size: 0.95rem;
      border: 1px solid var(--divider-color, #ccc);
      border-radius: 3px;
      background: var(--card-background-color, #fff);
      color: inherit;
    }
    .editor .time-colon {
      font-weight: 600; font-size: 1.4rem;
      margin: 0 2px;
    }
    .days-row { display: flex; flex-wrap: wrap; gap: 4px; }
    .day-toggle {
      padding: 6px 10px;
      border: 1px solid var(--divider-color, #ccc);
      background: var(--card-background-color, #fff);
      color: var(--secondary-text-color, #555);
      border-radius: 3px;
      cursor: pointer;
      font-family: inherit;
      font-size: 0.82rem;
    }
    .day-toggle.active {
      background: var(--primary-color, #03a9f4);
      color: var(--text-primary-color, #fff);
      border-color: transparent;
    }
    .conditions-readonly {
      padding: 10px 12px;
      background: var(--secondary-background-color, #f5f5f5);
      border-radius: 4px;
      font-size: 0.82rem;
      color: var(--secondary-text-color, #666);
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "omni-panel-programs": OmniPanelPrograms;
  }
}
