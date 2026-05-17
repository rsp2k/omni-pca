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
  ARG_TYPES,
  COMMAND_OPTIONS,
  COND_OPS,
  CommandOption,
  CondFamily,
  DAY_BITS,
  DecodedCondition,
  DecodedEvent,
  DecodedStructuredAnd,
  EventCategory,
  FIELDS_BY_TYPE,
  FIXED_EVENTS,
  Hass,
  MISC_CONDITIONALS,
  MONTH_NAMES,
  NamedObject,
  ObjectListResponse,
  PROGRAM_TYPE_AT,
  PROGRAM_TYPE_EVENT,
  PROGRAM_TYPE_EVERY,
  PROGRAM_TYPE_OR,
  PROGRAM_TYPE_TIMED,
  PROGRAM_TYPE_WHEN,
  PROGRAM_TYPE_YEARLY,
  ProgramDetail,
  ProgramFields,
  ProgramListResponse,
  ProgramRow,
  SECURITY_MODE_NAMES,
  argTypeKind,
  commandOptionFor,
  decodeAndCondition,
  decodeCondition,
  decodeEventId,
  decodeStructuredAnd,
  emptyAndRecord,
  emptyOrRecord,
  emptyThenRecord,
  encodeAndCondition,
  encodeCondition,
  encodeEventId,
  encodeStructuredAnd,
  eventIdFromFields,
  isEditableStructuredAnd,
  isStructuredAnd,
  isUnaryOp,
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
  // Separate edit state for clausal chains. Set when the user clicks
  // "Edit chain"; null otherwise. Head + conditions + actions are kept
  // as parallel arrays so add/remove operations on a specific list
  // don't churn the others. ``headSlot`` is the chain's anchor; the
  // backend writes the new chain into [headSlot, headSlot+len) and
  // clears any old slots beyond.
  @state() private _chainDraft: {
    headSlot: number;
    head: ProgramFields;
    conditions: ProgramFields[];
    actions: ProgramFields[];
  } | null = null;

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
    //
    // NOTE: this runs fire-and-forget from connectedCallback (and from
    // the hass-update path), so we need to kick off the initial list
    // *here*, after _entryId lands. Earlier versions checked _entryId
    // synchronously right after calling discover, which always saw
    // null and silently skipped loadList — the panel rendered "no
    // programs" forever until the first manual refresh.
    try {
      const entries = await this.hass.connection.sendMessagePromise<
        Array<{ entry_id: string; domain: string; title: string; state?: string }>
      >({ type: "config_entries/get" });
      const ours = entries.filter((e) => e.domain === "omni_pca");
      if (ours.length === 0) {
        this._error = "No Omni panel configured. Add one via Settings → Devices & Services.";
        return;
      }
      // Prefer entries that are actually loaded — a config entry in
      // setup_retry or migration_error has no live coordinator, and
      // the websocket commands return "panel not configured" against
      // it. Multi-loaded-panel installs still pick the first loaded
      // one; a multi-panel selector is a follow-up.
      const loaded = ours.find((e) => e.state === "loaded");
      this._entryId = (loaded ?? ours[0]).entry_id;
      this._error = null;
      // Kick off the initial list + start the live-state refresh timer.
      // Both are safe to call from here regardless of whether the
      // caller (connectedCallback / updated) also expected to start
      // them; _loadList is reentrant-safe and _startRefreshTimer is
      // idempotent.
      void this._loadList();
      this._startRefreshTimer();
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
    if (!this._detail) return;
    await this._ensureObjectsLoaded();
    if (!this._entryId) return;
    if (this._detail.kind === "chain") {
      this._beginChainEdit();
      return;
    }
    // The frontend supports editing compact-form TIMED / EVENT / YEARLY
    // programs. Other compact types (REMARK) remain read-only — the
    // editor pathway returns early without seeding a draft so the
    // read-only view stays visible.
    if (!EDITABLE_PROG_TYPES.has(this._detail.trigger_type)) return;
    const fields = this._detail.fields ?? this._defaultFieldsForType(
      this._detail.trigger_type,
    );
    if (fields === null) return;
    this._editingDraft = { ...fields };
    this._stopRefreshTimer();
  }

  private _beginChainEdit(): void {
    if (!this._detail || !this._detail.chain_members) return;
    const members = this._detail.chain_members;
    const head = members.find((m) => m.role === "head");
    if (!head) return;
    this._chainDraft = {
      headSlot: head.slot,
      head: { ...head.fields },
      conditions: members
        .filter((m) => m.role === "condition")
        .map((m) => ({ ...m.fields })),
      actions: members
        .filter((m) => m.role === "action")
        .map((m) => ({ ...m.fields })),
    };
    this._stopRefreshTimer();
  }

  private _cancelChainEdit(): void {
    this._chainDraft = null;
    this._startRefreshTimer();
  }

  private async _saveChainDraft(): Promise<void> {
    if (!this._chainDraft || !this._entryId) return;
    this._writeFeedback = "saving chain…";
    try {
      await this.hass.connection.sendMessagePromise({
        type: "omni_pca/programs/chain/write",
        entry_id: this._entryId,
        head_slot: this._chainDraft.headSlot,
        head: this._chainDraft.head,
        conditions: this._chainDraft.conditions,
        actions: this._chainDraft.actions,
      });
      this._writeFeedback = `saved chain @ slot ${this._chainDraft.headSlot}`;
      const headSlot = this._chainDraft.headSlot;
      this._chainDraft = null;
      this._startRefreshTimer();
      await this._loadList();
      await this._loadDetail(headSlot);
    } catch (err) {
      const m = err instanceof Error ? err.message : String(err);
      this._writeFeedback = `error: ${m}`;
    }
    setTimeout(() => { this._writeFeedback = null; }, 4000);
  }

  // ---- chain draft mutation helpers ---------------------------------

  private _patchChainHead(patch: Partial<ProgramFields>): void {
    if (!this._chainDraft) return;
    this._chainDraft = {
      ...this._chainDraft,
      head: { ...this._chainDraft.head, ...patch },
    };
  }

  private _patchChainCondition(idx: number, patch: Partial<ProgramFields>): void {
    if (!this._chainDraft) return;
    const conds = [...this._chainDraft.conditions];
    conds[idx] = { ...conds[idx], ...patch };
    this._chainDraft = { ...this._chainDraft, conditions: conds };
  }

  private _addChainCondition(asOr: boolean = false): void {
    if (!this._chainDraft) return;
    const fresh = asOr ? emptyOrRecord() : emptyAndRecord();
    this._chainDraft = {
      ...this._chainDraft,
      conditions: [...this._chainDraft.conditions, fresh],
    };
  }

  private _removeChainCondition(idx: number): void {
    if (!this._chainDraft) return;
    const conds = this._chainDraft.conditions.filter((_, i) => i !== idx);
    this._chainDraft = { ...this._chainDraft, conditions: conds };
  }

  private _patchChainAction(idx: number, patch: Partial<ProgramFields>): void {
    if (!this._chainDraft) return;
    const actions = [...this._chainDraft.actions];
    actions[idx] = { ...actions[idx], ...patch };
    this._chainDraft = { ...this._chainDraft, actions: actions };
  }

  private _addChainAction(): void {
    if (!this._chainDraft) return;
    const firstUnit = this._objects?.units?.[0]?.index ?? 1;
    this._chainDraft = {
      ...this._chainDraft,
      actions: [...this._chainDraft.actions, emptyThenRecord(firstUnit)],
    };
  }

  private _removeChainAction(idx: number): void {
    if (!this._chainDraft) return;
    // Guard: a chain must have at least one action.
    if (this._chainDraft.actions.length <= 1) return;
    const actions = this._chainDraft.actions.filter((_, i) => i !== idx);
    this._chainDraft = { ...this._chainDraft, actions: actions };
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
      case "zone":       return this._objects.zones;
      case "unit":       return this._objects.units;
      case "area":       return this._objects.areas;
      case "button":     return this._objects.buttons;
      case "thermostat": return this._objects.thermostats;
      default: return null;
    }
  }

  /** Augment a bucket with a "preserve original" option if ``current``
   * isn't represented by any entry. Real-world programs reference object
   * indexes far past the discovered range (e.g. raw byte values from
   * undecoded extended-output addressing on Omni Pro II — Unit 33025).
   * Without this option the form silently coerces such values to the
   * first known entry, which looks like the user already selected it.
   *
   * The synthesized entry is prepended so it's visually distinct (top
   * of the list) and labelled to make the situation obvious.
   */
  private _bucketWithPreserve(
    bucket: NamedObject[] | null,
    kind: string,
    current: number,
  ): NamedObject[] {
    const list = bucket ?? [];
    if (current === 0) return list;            // 0 = "no object", no synth
    if (list.some((o) => o.index === current)) return list;
    return [
      {
        index: current,
        name: `(undiscovered ${kind} ${current} — preserve original)`,
      },
      ...list,
    ];
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
    if (this._chainDraft !== null) {
      return this._renderChainEditor(d);
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
          ${
            (d.kind === "compact" && EDITABLE_PROG_TYPES.has(d.trigger_type))
            || d.kind === "chain" ? html`
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
          ${this._renderConditionsSection(draft)}
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
      const buttons = this._bucketWithPreserve(
        this._objects?.buttons ?? null, "button", decoded.button ?? 0,
      );
      return html`
        <label class="block">
          Button
          <select @change=${this._onEventButtonChange}>
            ${buttons.map((b) => html`
              <option .value=${String(b.index)}
                      ?selected=${b.index === decoded.button}>
                #${b.index} ${b.name}
              </option>
            `)}
          </select>
        </label>`;
    }
    if (decoded.category === "zone") {
      const zones = this._bucketWithPreserve(
        this._objects?.zones ?? null, "zone", decoded.zone ?? 0,
      );
      return html`
        <label class="block">
          Zone
          <select @change=${this._onEventZoneChange}>
            ${zones.map((z) => html`
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
      const units = this._bucketWithPreserve(
        this._objects?.units ?? null, "unit", decoded.unit ?? 0,
      );
      return html`
        <label class="block">
          Unit
          <select @change=${this._onEventUnitChange}>
            ${units.map((u) => html`
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
    const objectBucket = cmdOpt?.ref_kind
      ? this._bucketWithPreserve(
          this._pickBucket(cmdOpt.ref_kind),
          cmdOpt.ref_kind,
          draft.pr2 ?? 0,
        )
      : null;
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

  private _renderConditionsSection(draft: ProgramFields): TemplateResult {
    return html`
      <fieldset>
        <legend>Inline AND-IF conditions</legend>
        ${this._renderConditionSlot(
          "First condition", draft.cond ?? 0,
          (v) => this._patchDraft({ cond: v }),
        )}
        ${this._renderConditionSlot(
          "Second condition", draft.cond2 ?? 0,
          (v) => this._patchDraft({ cond2: v }),
        )}
      </fieldset>
    `;
  }

  private _renderConditionSlot(
    label: string, raw: number, onChange: (newCond: number) => void,
  ): TemplateResult {
    const decoded = decodeCondition(raw);
    const setFamily = (family: CondFamily) => {
      // Seed sensible defaults when switching family so the sub-fields
      // immediately encode to a non-degenerate value.
      const firstZone = this._objects?.zones?.[0]?.index ?? 1;
      const firstUnit = this._objects?.units?.[0]?.index ?? 1;
      const firstArea = this._objects?.areas?.[0]?.index ?? 1;
      let next: DecodedCondition;
      switch (family) {
        case "none":  next = { family: "none" }; break;
        case "misc":  next = { family: "misc", misc: 1 }; break; // NEVER
        case "zone":  next = { family: "zone", index: firstZone, active: false }; break;
        case "unit":  next = { family: "unit", index: firstUnit, active: true }; break;
        case "time":  next = { family: "time", index: 1, active: true }; break;
        case "sec":   next = { family: "sec", index: firstArea, mode: 0 }; break;
      }
      onChange(encodeCondition(next));
    };
    return html`
      <div class="cond-slot">
        <label class="block cond-family-label">
          ${label}
          <select @change=${(e: Event) =>
            setFamily((e.target as HTMLSelectElement).value as CondFamily)}>
            <option value="none" ?selected=${decoded.family === "none"}>(none)</option>
            <option value="zone" ?selected=${decoded.family === "zone"}>Zone state</option>
            <option value="unit" ?selected=${decoded.family === "unit"}>Unit state</option>
            <option value="sec"  ?selected=${decoded.family === "sec"}>Area in security mode</option>
            <option value="time" ?selected=${decoded.family === "time"}>Time clock</option>
            <option value="misc" ?selected=${decoded.family === "misc"}>Misc (light, AC power, …)</option>
          </select>
        </label>
        ${this._renderConditionSubfields(decoded, onChange)}
      </div>
    `;
  }

  private _renderConditionSubfields(
    decoded: DecodedCondition, onChange: (newCond: number) => void,
  ): TemplateResult {
    if (decoded.family === "none") return html``;
    if (decoded.family === "zone") {
      const zones = this._bucketWithPreserve(
        this._objects?.zones ?? null, "zone", decoded.index ?? 0,
      );
      return html`
        <label class="block">
          Zone
          <select @change=${(e: Event) => {
            const idx = parseInt((e.target as HTMLSelectElement).value, 10);
            onChange(encodeCondition({ ...decoded, index: idx }));
          }}>
            ${zones.map((z) => html`
              <option .value=${String(z.index)}
                      ?selected=${z.index === decoded.index}>
                #${z.index} ${z.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Is
          <select @change=${(e: Event) => {
            const active = (e.target as HTMLSelectElement).value === "1";
            onChange(encodeCondition({ ...decoded, active }));
          }}>
            <option value="0" ?selected=${!decoded.active}>secure</option>
            <option value="1" ?selected=${decoded.active}>not ready</option>
          </select>
        </label>`;
    }
    if (decoded.family === "unit") {
      const units = this._bucketWithPreserve(
        this._objects?.units ?? null, "unit", decoded.index ?? 0,
      );
      return html`
        <label class="block">
          Unit
          <select @change=${(e: Event) => {
            const idx = parseInt((e.target as HTMLSelectElement).value, 10);
            onChange(encodeCondition({ ...decoded, index: idx }));
          }}>
            ${units.map((u) => html`
              <option .value=${String(u.index)}
                      ?selected=${u.index === decoded.index}>
                #${u.index} ${u.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Is
          <select @change=${(e: Event) => {
            const active = (e.target as HTMLSelectElement).value === "1";
            onChange(encodeCondition({ ...decoded, active }));
          }}>
            <option value="1" ?selected=${decoded.active}>ON</option>
            <option value="0" ?selected=${!decoded.active}>OFF</option>
          </select>
        </label>`;
    }
    if (decoded.family === "sec") {
      const areas = this._bucketWithPreserve(
        this._objects?.areas ?? null, "area", decoded.index ?? 0,
      );
      return html`
        <label class="block">
          Area
          <select @change=${(e: Event) => {
            const idx = parseInt((e.target as HTMLSelectElement).value, 10);
            onChange(encodeCondition({ ...decoded, index: idx }));
          }}>
            ${areas.map((a) => html`
              <option .value=${String(a.index)}
                      ?selected=${a.index === decoded.index}>
                #${a.index} ${a.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Mode
          <select @change=${(e: Event) => {
            const mode = parseInt((e.target as HTMLSelectElement).value, 10);
            onChange(encodeCondition({ ...decoded, mode }));
          }}>
            ${SECURITY_MODE_NAMES.map((m) => html`
              <option .value=${String(m.value)}
                      ?selected=${m.value === decoded.mode}>
                ${m.label}
              </option>
            `)}
          </select>
        </label>`;
    }
    if (decoded.family === "time") {
      return html`
        <label class="block">
          Time clock # (1..3)
          <input
            type="number" min="1" max="3"
            .value=${String(decoded.index ?? 1)}
            @input=${(e: Event) => {
              const idx = parseInt((e.target as HTMLInputElement).value, 10);
              if (Number.isFinite(idx)) {
                onChange(encodeCondition({ ...decoded, index: idx }));
              }
            }}
          />
        </label>
        <label class="block">
          Is
          <select @change=${(e: Event) => {
            const active = (e.target as HTMLSelectElement).value === "1";
            onChange(encodeCondition({ ...decoded, active }));
          }}>
            <option value="1" ?selected=${decoded.active}>enabled</option>
            <option value="0" ?selected=${!decoded.active}>disabled</option>
          </select>
        </label>`;
    }
    // misc
    return html`
      <label class="block">
        Condition
        <select @change=${(e: Event) => {
          const misc = parseInt((e.target as HTMLSelectElement).value, 10);
          onChange(encodeCondition({ family: "misc", misc }));
        }}>
          ${MISC_CONDITIONALS.map((m) => html`
            <option .value=${String(m.value)}
                    ?selected=${m.value === decoded.misc}>
              ${m.label}
            </option>
          `)}
        </select>
      </label>`;
  }

  // ---- clausal chain editor -----------------------------------------

  private _renderChainEditor(d: ProgramDetail): TemplateResult {
    const draft = this._chainDraft!;
    return html`
      <aside class="detail editor">
        <header>
          <div>
            <span class="trigger-badge trigger-${d.trigger_type.toLowerCase()}">
              EDIT • ${d.trigger_type}
            </span>
            <span class="slot">head @ slot #${draft.headSlot}</span>
          </div>
          <button type="button" class="close" @click=${this._cancelChainEdit}>×</button>
        </header>

        <div class="editor-body">
          ${this._renderChainHeadSection(draft.head)}
          ${this._renderChainConditionsSection(draft.conditions)}
          ${this._renderChainActionsSection(draft.actions)}
          <div class="chain-meta">
            Chain will occupy <strong>${1 + draft.conditions.length + draft.actions.length}</strong>
            consecutive slots starting at #${draft.headSlot}.
          </div>
        </div>

        <footer>
          <button type="button" class="primary" @click=${this._saveChainDraft}>
            Save chain
          </button>
          <button type="button" class="secondary" @click=${this._cancelChainEdit}>
            Cancel
          </button>
          ${this._writeFeedback ? html`
            <span class="fire-feedback">${this._writeFeedback}</span>` : ""}
        </footer>
      </aside>
    `;
  }

  private _renderChainHeadSection(head: ProgramFields): TemplateResult {
    // Reuse the existing trigger-section renderers from the compact-form
    // editor — head records share the same field semantics as their
    // compact counterparts (AT = TIMED, WHEN = EVENT, EVERY uses
    // cond/cond2 for interval). The renderers patch via _patchDraft,
    // which we shim to redirect to _patchChainHead while in chain mode.
    if (head.prog_type === PROGRAM_TYPE_WHEN) {
      return this._renderEventTriggerChain(head);
    }
    if (head.prog_type === PROGRAM_TYPE_AT) {
      return this._renderTimedTriggerChain(head);
    }
    if (head.prog_type === PROGRAM_TYPE_EVERY) {
      return this._renderEveryTriggerChain(head);
    }
    return html`
      <div class="conditions-readonly">
        Editing trigger type ${head.prog_type} (chain head) is not supported.
      </div>`;
  }

  private _renderTimedTriggerChain(head: ProgramFields): TemplateResult {
    // Same fields as TIMED compact: hour/minute/days.
    return html`
      <fieldset>
        <legend>AT (trigger)</legend>
        <div class="row">
          <label>
            Hour
            <input type="number" min="0" max="23"
              .value=${String(head.hour ?? 0)}
              @input=${(e: Event) => this._patchChainHead({
                hour: parseInt((e.target as HTMLInputElement).value, 10) || 0,
              })}
            />
          </label>
          <span class="time-colon">:</span>
          <label>
            Minute
            <input type="number" min="0" max="59"
              .value=${String(head.minute ?? 0)}
              @input=${(e: Event) => this._patchChainHead({
                minute: parseInt((e.target as HTMLInputElement).value, 10) || 0,
              })}
            />
          </label>
        </div>
        <div class="days-row">
          ${DAY_BITS.map((d) => {
            const active = ((head.days ?? 0) & d.bit) !== 0;
            return html`
              <button type="button"
                class="day-toggle ${active ? "active" : ""}"
                @click=${() => this._patchChainHead({
                  days: (head.days ?? 0) ^ d.bit,
                })}
              >${d.label}</button>`;
          })}
        </div>
      </fieldset>
    `;
  }

  private _renderEventTriggerChain(head: ProgramFields): TemplateResult {
    // WHEN heads use the same event-id packing as EVENT compact-form:
    // month/day bytes carry (event_id >> 8) and (event_id & 0xFF).
    const eventId = ((head.month ?? 0) << 8) | (head.day ?? 0);
    const decoded = decodeEventId(eventId);
    const setEvent = (e: DecodedEvent) => {
      const id = encodeEventId(e);
      this._patchChainHead({
        month: (id >> 8) & 0xFF,
        day: id & 0xFF,
      });
    };
    return html`
      <fieldset>
        <legend>WHEN (trigger event)</legend>
        <label class="block">
          Category
          <select @change=${(e: Event) => {
            const cat = (e.target as HTMLSelectElement).value as EventCategory;
            if (cat === "button") {
              const fb = this._objects?.buttons?.[0]?.index ?? 1;
              setEvent({ category: "button", button: fb });
            } else if (cat === "zone") {
              const fz = this._objects?.zones?.[0]?.index ?? 1;
              setEvent({ category: "zone", zone: fz, zoneState: 1 });
            } else if (cat === "unit") {
              const fu = this._objects?.units?.[0]?.index ?? 1;
              setEvent({ category: "unit", unit: fu, unitOn: true });
            } else if (cat === "fixed") {
              setEvent({ category: "fixed", fixedId: 772 });
            }
          }}>
            <option value="button" ?selected=${decoded.category === "button"}>Button press</option>
            <option value="zone" ?selected=${decoded.category === "zone"}>Zone state change</option>
            <option value="unit" ?selected=${decoded.category === "unit"}>Unit state change</option>
            <option value="fixed" ?selected=${decoded.category === "fixed"}>Fixed (phone / AC)</option>
            ${decoded.category === "raw" ? html`
              <option value="raw" selected>Raw 0x${eventId.toString(16).padStart(4, "0")}</option>` : ""}
          </select>
        </label>
        ${this._renderChainEventSubfields(decoded, setEvent)}
      </fieldset>
    `;
  }

  private _renderChainEventSubfields(
    decoded: DecodedEvent, setEvent: (e: DecodedEvent) => void,
  ): TemplateResult {
    if (decoded.category === "button") {
      const buttons = this._bucketWithPreserve(
        this._objects?.buttons ?? null, "button", decoded.button ?? 0,
      );
      return html`
        <label class="block">
          Button
          <select @change=${(e: Event) => setEvent({
            category: "button",
            button: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${buttons.map((b) => html`
              <option .value=${String(b.index)}
                      ?selected=${b.index === decoded.button}>
                #${b.index} ${b.name}
              </option>
            `)}
          </select>
        </label>`;
    }
    if (decoded.category === "zone") {
      const zones = this._bucketWithPreserve(
        this._objects?.zones ?? null, "zone", decoded.zone ?? 0,
      );
      return html`
        <label class="block">
          Zone
          <select @change=${(e: Event) => setEvent({
            ...decoded, category: "zone",
            zone: parseInt((e.target as HTMLSelectElement).value, 10),
            zoneState: decoded.zoneState ?? 1,
          })}>
            ${zones.map((z) => html`
              <option .value=${String(z.index)}
                      ?selected=${z.index === decoded.zone}>
                #${z.index} ${z.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Becomes
          <select @change=${(e: Event) => setEvent({
            ...decoded, category: "zone",
            zone: decoded.zone ?? 1,
            zoneState: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            <option value="0" ?selected=${decoded.zoneState === 0}>secure</option>
            <option value="1" ?selected=${decoded.zoneState === 1}>not ready</option>
            <option value="2" ?selected=${decoded.zoneState === 2}>trouble</option>
            <option value="3" ?selected=${decoded.zoneState === 3}>tamper</option>
          </select>
        </label>`;
    }
    if (decoded.category === "unit") {
      const units = this._bucketWithPreserve(
        this._objects?.units ?? null, "unit", decoded.unit ?? 0,
      );
      return html`
        <label class="block">
          Unit
          <select @change=${(e: Event) => setEvent({
            ...decoded, category: "unit",
            unit: parseInt((e.target as HTMLSelectElement).value, 10),
            unitOn: decoded.unitOn ?? true,
          })}>
            ${units.map((u) => html`
              <option .value=${String(u.index)}
                      ?selected=${u.index === decoded.unit}>
                #${u.index} ${u.name}
              </option>
            `)}
          </select>
        </label>
        <label class="block">
          Turns
          <select @change=${(e: Event) => setEvent({
            ...decoded, category: "unit",
            unit: decoded.unit ?? 1,
            unitOn: (e.target as HTMLSelectElement).value === "1",
          })}>
            <option value="1" ?selected=${decoded.unitOn === true}>ON</option>
            <option value="0" ?selected=${decoded.unitOn === false}>OFF</option>
          </select>
        </label>`;
    }
    if (decoded.category === "fixed") {
      return html`
        <label class="block">
          Event
          <select @change=${(e: Event) => setEvent({
            category: "fixed",
            fixedId: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${FIXED_EVENTS.map((f) => html`
              <option .value=${String(f.id)}
                      ?selected=${f.id === decoded.fixedId}>
                ${f.label}
              </option>
            `)}
          </select>
        </label>`;
    }
    return html`<div class="conditions-readonly">Unrecognised event ID. Pick a category to redefine.</div>`;
  }

  private _renderEveryTriggerChain(head: ProgramFields): TemplateResult {
    // EVERY's interval = ((cond & 0xFF) << 8) | ((cond2 >> 8) & 0xFF).
    // Decode for display; the editor exposes a single "seconds" input
    // and packs back to cond + cond2 on change.
    const interval =
      (((head.cond ?? 0) & 0xFF) << 8) | (((head.cond2 ?? 0) >> 8) & 0xFF);
    return html`
      <fieldset>
        <legend>EVERY (interval, seconds)</legend>
        <label class="block">
          Seconds between fires
          <input type="number" min="1" max="65535"
            .value=${String(interval || 1)}
            @input=${(e: Event) => {
              const sec = parseInt((e.target as HTMLInputElement).value, 10);
              if (!Number.isFinite(sec) || sec < 1) return;
              this._patchChainHead({
                cond: (sec >> 8) & 0xFF,
                cond2: (sec & 0xFF) << 8,
              });
            }}
          />
        </label>
      </fieldset>
    `;
  }

  private _renderChainConditionsSection(conds: ProgramFields[]): TemplateResult {
    return html`
      <fieldset>
        <legend>
          Conditions (${conds.length})
          <button type="button" class="mini-btn" @click=${() => this._addChainCondition(false)}>
            + AND IF
          </button>
          <button type="button" class="mini-btn" @click=${() => this._addChainCondition(true)}>
            + OR IF
          </button>
        </legend>
        ${conds.length === 0 ? html`
          <div class="conditions-readonly">
            No conditions — chain fires unconditionally when triggered.
          </div>` : ""}
        ${conds.map((c, idx) => this._renderChainConditionRow(c, idx))}
      </fieldset>
    `;
  }

  private _renderChainConditionRow(
    cond: ProgramFields, idx: number,
  ): TemplateResult {
    const isOr = cond.prog_type === PROGRAM_TYPE_OR;
    if (isStructuredAnd(cond)) {
      return this._renderStructuredChainConditionRow(cond, idx, isOr);
    }
    const decoded = decodeAndCondition(cond);
    return html`
      <div class="cond-slot">
        <div class="cond-row-header">
          <strong>${isOr ? "OR IF" : "AND IF"}</strong>
          <button type="button" class="mini-btn danger"
            @click=${() => this._removeChainCondition(idx)}>×</button>
        </div>
        ${this._renderChainCondFamily(decoded, idx)}
      </div>`;
  }

  private _renderStructuredChainConditionRow(
    cond: ProgramFields, idx: number, isOr: boolean,
  ): TemplateResult {
    const s = decodeStructuredAnd(cond);
    if (!isEditableStructuredAnd(s)) {
      // Out of editor scope (non-constant Arg2, unsupported Arg1 type,
      // or non-zero compConst). Surface as preserve-only so the user
      // can still remove the row but can't damage the encoded data.
      return html`
        <div class="cond-slot structured-cond">
          <div class="cond-row-header">
            <strong>${isOr ? "OR IF" : "AND IF"}</strong>
            <span class="readonly-tag">read-only</span>
            <button type="button" class="mini-btn danger"
              @click=${() => this._removeChainCondition(idx)}>×</button>
          </div>
          <div class="conditions-readonly">
            Structured comparison with a shape the editor can't drive
            yet (Arg2 references another object, Arg1 is an unsupported
            type, or a CompConst value is present). Preserved on save.
          </div>
        </div>`;
    }
    return html`
      <div class="cond-slot structured-cond">
        <div class="cond-row-header">
          <strong>${isOr ? "OR IF" : "AND IF"}</strong>
          <span class="structured-tag">structured</span>
          <button type="button" class="mini-btn danger"
            @click=${() => this._removeChainCondition(idx)}>×</button>
        </div>
        ${this._renderStructuredAndForm(s, idx)}
      </div>`;
  }

  /** Render the editor for one structured-AND condition. Lays out as:
   *
   *      Arg1 type ▸ object/picker ▸ field ▸ operator ▸ Arg2 constant
   *
   *  Arg2 is locked to Constant in this pass. For unary operators
   *  (ODD / EVEN) the Arg2 input is hidden.
   */
  private _renderStructuredAndForm(
    s: DecodedStructuredAnd, idx: number,
  ): TemplateResult {
    const update = (patch: Partial<DecodedStructuredAnd>) => {
      const merged = { ...s, ...patch };
      // Force Arg2 = Constant in editor scope so nothing accidentally
      // promotes to an object reference.
      merged.arg2Type = 0;
      merged.arg2Field = 0;
      this._patchChainCondition(idx, encodeStructuredAnd(merged));
    };
    const arg1Fields = FIELDS_BY_TYPE[s.arg1Type] ?? [];
    const arg1Kind = argTypeKind(s.arg1Type);
    const showArg2 = !isUnaryOp(s.op);
    return html`
      <div class="structured-row">
        <label class="block">
          Arg1 type
          <select @change=${(e: Event) => {
            const newType = parseInt((e.target as HTMLSelectElement).value, 10);
            // Reset arg1Ix + field when type changes — keeps the form
            // self-consistent and avoids stale picker values.
            const firstField = (FIELDS_BY_TYPE[newType] ?? [{ value: 0 }])[0].value;
            const newKind = argTypeKind(newType);
            let newIx = 0;
            if (newKind === "zone") newIx = this._objects?.zones?.[0]?.index ?? 1;
            else if (newKind === "unit") newIx = this._objects?.units?.[0]?.index ?? 1;
            else if (newKind === "thermostat") newIx = this._objects?.thermostats?.[0]?.index ?? 1;
            else if (newKind === "area") newIx = this._objects?.areas?.[0]?.index ?? 1;
            update({
              arg1Type: newType,
              arg1Ix: newIx,
              arg1Field: firstField,
            });
          }}>
            ${ARG_TYPES.filter((a) => a.value !== 0).map((a) => html`
              <option .value=${String(a.value)} ?selected=${a.value === s.arg1Type}>
                ${a.label}
              </option>`)}
          </select>
        </label>

        ${arg1Kind ? this._renderStructuredArg1Picker(s, arg1Kind, update) : ""}

        ${arg1Fields.length > 0 ? html`
          <label class="block">
            Field
            <select @change=${(e: Event) => update({
              arg1Field: parseInt((e.target as HTMLSelectElement).value, 10),
            })}>
              ${arg1Fields.map((f) => html`
                <option .value=${String(f.value)} ?selected=${f.value === s.arg1Field}>
                  ${f.label}
                </option>`)}
            </select>
          </label>` : ""}

        <label class="block">
          Operator
          <select @change=${(e: Event) => update({
            op: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${COND_OPS.map((o) => html`
              <option .value=${String(o.value)} ?selected=${o.value === s.op}>
                ${o.label}
              </option>`)}
          </select>
        </label>

        ${showArg2 ? html`
          <label class="block">
            Compare against (constant)
            <input type="number" min="0" max="65535"
              .value=${String(s.arg2Ix)}
              @input=${(e: Event) => {
                const v = parseInt((e.target as HTMLInputElement).value, 10);
                if (Number.isFinite(v) && v >= 0 && v <= 0xFFFF) {
                  update({ arg2Ix: v });
                }
              }}
            />
          </label>` : ""}
      </div>`;
  }

  private _renderStructuredArg1Picker(
    s: DecodedStructuredAnd,
    kind: string,
    update: (p: Partial<DecodedStructuredAnd>) => void,
  ): TemplateResult {
    const bucket = this._bucketWithPreserve(
      this._pickBucket(kind), kind, s.arg1Ix,
    );
    const label = kind[0].toUpperCase() + kind.slice(1);
    return html`
      <label class="block">
        ${label}
        <select @change=${(e: Event) => update({
          arg1Ix: parseInt((e.target as HTMLSelectElement).value, 10),
        })}>
          ${bucket.map((o) => html`
            <option .value=${String(o.index)} ?selected=${o.index === s.arg1Ix}>
              #${o.index} ${o.name}
            </option>`)}
        </select>
      </label>`;
  }

  private _renderChainCondFamily(
    decoded: DecodedCondition, idx: number,
  ): TemplateResult {
    const setFamily = (family: CondFamily) => {
      const firstZone = this._objects?.zones?.[0]?.index ?? 1;
      const firstUnit = this._objects?.units?.[0]?.index ?? 1;
      const firstArea = this._objects?.areas?.[0]?.index ?? 1;
      let next: DecodedCondition;
      switch (family) {
        case "none":  next = { family: "none" }; break;
        case "misc":  next = { family: "misc", misc: 1 }; break;
        case "zone":  next = { family: "zone", index: firstZone, active: false }; break;
        case "unit":  next = { family: "unit", index: firstUnit, active: true }; break;
        case "time":  next = { family: "time", index: 1, active: true }; break;
        case "sec":   next = { family: "sec", index: firstArea, mode: 0 }; break;
      }
      const enc = encodeAndCondition(next);
      this._patchChainCondition(idx, enc);
    };
    const setDecoded = (next: DecodedCondition) => {
      this._patchChainCondition(idx, encodeAndCondition(next));
    };
    return html`
      <label class="block">
        Family
        <select @change=${(e: Event) =>
          setFamily((e.target as HTMLSelectElement).value as CondFamily)}>
          <option value="zone" ?selected=${decoded.family === "zone"}>Zone state</option>
          <option value="unit" ?selected=${decoded.family === "unit"}>Unit state</option>
          <option value="sec"  ?selected=${decoded.family === "sec"}>Area in security mode</option>
          <option value="time" ?selected=${decoded.family === "time"}>Time clock</option>
          <option value="misc" ?selected=${decoded.family === "misc"}>Misc</option>
        </select>
      </label>
      ${this._renderChainCondSubfields(decoded, setDecoded)}
    `;
  }

  private _renderChainCondSubfields(
    decoded: DecodedCondition, setDecoded: (d: DecodedCondition) => void,
  ): TemplateResult {
    if (decoded.family === "zone") {
      const zones = this._bucketWithPreserve(
        this._objects?.zones ?? null, "zone", decoded.index ?? 0,
      );
      return html`
        <label class="block">
          Zone
          <select @change=${(e: Event) => setDecoded({
            ...decoded, index: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${zones.map((z) => html`
              <option .value=${String(z.index)} ?selected=${z.index === decoded.index}>
                #${z.index} ${z.name}
              </option>`)}
          </select>
        </label>
        <label class="block">
          Is
          <select @change=${(e: Event) => setDecoded({
            ...decoded, active: (e.target as HTMLSelectElement).value === "1",
          })}>
            <option value="0" ?selected=${!decoded.active}>secure</option>
            <option value="1" ?selected=${decoded.active}>not ready</option>
          </select>
        </label>`;
    }
    if (decoded.family === "unit") {
      const units = this._bucketWithPreserve(
        this._objects?.units ?? null, "unit", decoded.index ?? 0,
      );
      return html`
        <label class="block">
          Unit
          <select @change=${(e: Event) => setDecoded({
            ...decoded, index: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${units.map((u) => html`
              <option .value=${String(u.index)} ?selected=${u.index === decoded.index}>
                #${u.index} ${u.name}
              </option>`)}
          </select>
        </label>
        <label class="block">
          Is
          <select @change=${(e: Event) => setDecoded({
            ...decoded, active: (e.target as HTMLSelectElement).value === "1",
          })}>
            <option value="1" ?selected=${decoded.active}>ON</option>
            <option value="0" ?selected=${!decoded.active}>OFF</option>
          </select>
        </label>`;
    }
    if (decoded.family === "sec") {
      const areas = this._bucketWithPreserve(
        this._objects?.areas ?? null, "area", decoded.index ?? 0,
      );
      return html`
        <label class="block">
          Area
          <select @change=${(e: Event) => setDecoded({
            ...decoded, index: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${areas.map((a) => html`
              <option .value=${String(a.index)} ?selected=${a.index === decoded.index}>
                #${a.index} ${a.name}
              </option>`)}
          </select>
        </label>
        <label class="block">
          Mode
          <select @change=${(e: Event) => setDecoded({
            ...decoded, mode: parseInt((e.target as HTMLSelectElement).value, 10),
          })}>
            ${SECURITY_MODE_NAMES.map((m) => html`
              <option .value=${String(m.value)} ?selected=${m.value === decoded.mode}>
                ${m.label}
              </option>`)}
          </select>
        </label>`;
    }
    if (decoded.family === "time") {
      return html`
        <label class="block">
          Time clock # (1..3)
          <input type="number" min="1" max="3"
            .value=${String(decoded.index ?? 1)}
            @input=${(e: Event) => {
              const idx = parseInt((e.target as HTMLInputElement).value, 10);
              if (Number.isFinite(idx)) setDecoded({ ...decoded, index: idx });
            }}
          />
        </label>
        <label class="block">
          Is
          <select @change=${(e: Event) => setDecoded({
            ...decoded, active: (e.target as HTMLSelectElement).value === "1",
          })}>
            <option value="1" ?selected=${decoded.active}>enabled</option>
            <option value="0" ?selected=${!decoded.active}>disabled</option>
          </select>
        </label>`;
    }
    // misc
    return html`
      <label class="block">
        Condition
        <select @change=${(e: Event) => setDecoded({
          family: "misc",
          misc: parseInt((e.target as HTMLSelectElement).value, 10),
        })}>
          ${MISC_CONDITIONALS.map((m) => html`
            <option .value=${String(m.value)} ?selected=${m.value === decoded.misc}>
              ${m.label}
            </option>`)}
        </select>
      </label>`;
  }

  private _renderChainActionsSection(actions: ProgramFields[]): TemplateResult {
    return html`
      <fieldset>
        <legend>
          Actions (${actions.length})
          <button type="button" class="mini-btn"
            @click=${() => this._addChainAction()}>+ THEN</button>
        </legend>
        ${actions.map((a, idx) => this._renderChainActionRow(a, idx, actions.length))}
      </fieldset>
    `;
  }

  private _renderChainActionRow(
    action: ProgramFields, idx: number, total: number,
  ): TemplateResult {
    const cmdOpt: CommandOption | undefined = commandOptionFor(action.cmd ?? 0);
    const objectBucket = cmdOpt?.ref_kind
      ? this._bucketWithPreserve(
          this._pickBucket(cmdOpt.ref_kind),
          cmdOpt.ref_kind,
          action.pr2 ?? 0,
        )
      : null;
    const showsLevelPercent = action.cmd === 9;
    return html`
      <div class="cond-slot">
        <div class="cond-row-header">
          <strong>${idx === 0 ? "THEN" : "AND"}</strong>
          ${total > 1 ? html`
            <button type="button" class="mini-btn danger"
              @click=${() => this._removeChainAction(idx)}>×</button>` : ""}
        </div>
        <label class="block">
          Command
          <select @change=${(e: Event) => {
            const value = parseInt((e.target as HTMLSelectElement).value, 10);
            const opt = commandOptionFor(value);
            let newPr2 = action.pr2 ?? 0;
            if (opt?.ref_kind && this._objects) {
              const bucket = this._pickBucket(opt.ref_kind);
              if (bucket && bucket.length > 0 &&
                  !bucket.some((o) => o.index === newPr2)) {
                newPr2 = bucket[0].index;
              }
            } else if (!opt?.ref_kind) {
              newPr2 = 0;
            }
            this._patchChainAction(idx, { cmd: value, pr2: newPr2 });
          }}>
            ${COMMAND_OPTIONS.map((c) => html`
              <option .value=${String(c.value)} ?selected=${c.value === action.cmd}>
                ${c.label}
              </option>`)}
          </select>
        </label>
        ${cmdOpt?.ref_kind ? html`
          <label class="block">
            ${cmdOpt.ref_kind[0].toUpperCase() + cmdOpt.ref_kind.slice(1)}
            <select @change=${(e: Event) => {
              const v = parseInt((e.target as HTMLSelectElement).value, 10);
              if (Number.isFinite(v)) this._patchChainAction(idx, { pr2: v });
            }}>
              ${(objectBucket ?? []).map((o) => html`
                <option .value=${String(o.index)} ?selected=${o.index === action.pr2}>
                  #${o.index} ${o.name}
                </option>`)}
            </select>
          </label>` : ""}
        ${showsLevelPercent ? html`
          <label class="block">
            Level (0..100)
            <input type="number" min="0" max="100"
              .value=${String(action.par ?? 0)}
              @input=${(e: Event) => {
                const v = parseInt((e.target as HTMLInputElement).value, 10);
                if (Number.isFinite(v) && v >= 0 && v <= 100) {
                  this._patchChainAction(idx, { par: v });
                }
              }}
            />
          </label>` : ""}
      </div>
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
    .cond-slot {
      padding: 8px 10px;
      margin-top: 6px;
      background: var(--secondary-background-color, #f5f5f5);
      border-radius: 4px;
    }
    .cond-slot:first-of-type { margin-top: 0; }
    .cond-family-label {
      font-weight: 600;
      color: var(--primary-text-color, #000);
    }
    .cond-row-header {
      display: flex; justify-content: space-between; align-items: center;
      margin-bottom: 4px;
    }
    .mini-btn {
      border: 1px solid var(--divider-color, #ccc);
      background: var(--card-background-color, #fff);
      color: inherit;
      padding: 2px 8px;
      border-radius: 3px;
      font-size: 0.78rem;
      cursor: pointer;
      font-family: inherit;
      margin-left: 6px;
    }
    .mini-btn:hover { background: var(--secondary-background-color, #eee); }
    .mini-btn.danger {
      color: var(--error-color, #db4437);
      border-color: var(--error-color, #db4437);
    }
    .structured-cond {
      background: rgba(255, 152, 0, 0.08);  /* subtle structured tint */
    }
    .structured-row {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
    }
    .structured-tag, .readonly-tag {
      display: inline-block;
      margin-left: 6px;
      padding: 1px 6px;
      font-size: 0.7rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      border-radius: 3px;
    }
    .structured-tag {
      background: rgba(255, 152, 0, 0.18);
      color: #b35a00;
    }
    .readonly-tag {
      background: var(--secondary-background-color, #eee);
      color: var(--secondary-text-color, #888);
    }
    .chain-meta {
      margin-top: 8px;
      padding: 8px 10px;
      font-size: 0.82rem;
      color: var(--secondary-text-color, #666);
      background: var(--secondary-background-color, #f5f5f5);
      border-radius: 4px;
    }
  `;
}

declare global {
  interface HTMLElementTagNameMap {
    "omni-panel-programs": OmniPanelPrograms;
  }
}
