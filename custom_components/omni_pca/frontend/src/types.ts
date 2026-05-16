// TS mirrors of the Phase-B websocket wire shapes. Short field names
// match websocket.py's _tokens_to_json — keep these in sync if the
// Python side changes.

export interface Token {
  /** "keyword" / "operator" / "ref" / "value" / "text" / "indent" / "newline" */
  k: string;
  /** Display text for this token. Empty for newline. */
  t: string;
  /** Object kind for REF tokens (zone / unit / area / thermostat / button / message / code / timeclock). */
  ek?: string;
  /** 1-based slot for REF tokens. */
  ei?: number;
  /** Live-state badge for REF tokens (e.g. "SECURE", "ON 60%"). */
  s?: string;
}

export interface ProgramRow {
  /** 1-based slot number. For chains, the head slot. */
  slot: number;
  /** "compact" or "chain". */
  kind: string;
  /** TIMED / EVENT / YEARLY / WHEN / AT / EVERY / REMARK / FREE. */
  trigger_type: string;
  /** One-line summary token stream. */
  summary: Token[];
  /** Flat ["unit:7", "zone:5", ...] for filter chips. */
  references: string[];
  condition_count: number;
  action_count: number;
}

export interface ProgramListResponse {
  programs: ProgramRow[];
  total: number;
  filtered_total: number;
  offset: number;
  limit: number;
}

export interface ProgramDetail {
  slot: number;
  kind: string;
  trigger_type: string;
  /** Full structured-English token stream. */
  tokens: Token[];
  references: string[];
  /** For chain detail: every slot the chain spans. */
  chain_slots?: number[];
}

export interface ProgramListRequest {
  type: "omni_pca/programs/list";
  entry_id: string;
  trigger_types?: string[];
  references_entity?: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export interface ProgramGetRequest {
  type: "omni_pca/programs/get";
  entry_id: string;
  slot: number;
}

export interface ProgramFireRequest {
  type: "omni_pca/programs/fire";
  entry_id: string;
  slot: number;
}

// Raw Program dict — mirrors the dataclass on the Python side. Sent
// over the wire by ``omni_pca/programs/write``; the websocket validates
// each field's range and constructs the typed dataclass server-side.
export interface ProgramFields {
  prog_type: number;
  cond?: number;
  cond2?: number;
  cmd?: number;
  par?: number;
  pr2?: number;
  month?: number;
  day?: number;
  days?: number;
  hour?: number;
  minute?: number;
  remark_id?: number | null;
}

export interface ProgramWriteRequest {
  type: "omni_pca/programs/write";
  entry_id: string;
  slot: number;
  program: ProgramFields;
}

export interface NamedObject {
  index: number;
  name: string;
}

export interface ObjectListResponse {
  zones: NamedObject[];
  units: NamedObject[];
  areas: NamedObject[];
  thermostats: NamedObject[];
  buttons: NamedObject[];
}

// Command enum values we let the user pick from the editor. Mirrors the
// most useful subset of omni_pca.commands.Command. The second element
// is what object kind (if any) the command's pr2 parameter references —
// drives the object picker's filter.
export interface CommandOption {
  value: number;
  label: string;
  ref_kind: "unit" | "zone" | "area" | "button" | null;
}

export const COMMAND_OPTIONS: CommandOption[] = [
  { value: 0,  label: "Turn OFF unit",        ref_kind: "unit" },
  { value: 1,  label: "Turn ON unit",         ref_kind: "unit" },
  { value: 2,  label: "All OFF",              ref_kind: null },
  { value: 3,  label: "All ON",               ref_kind: null },
  { value: 4,  label: "Bypass zone",          ref_kind: "zone" },
  { value: 5,  label: "Restore zone",         ref_kind: "zone" },
  { value: 7,  label: "Execute button",       ref_kind: "button" },
  { value: 9,  label: "Set unit level %",     ref_kind: "unit" },
  { value: 48, label: "Disarm area",          ref_kind: "area" },
  { value: 49, label: "Arm area Day",         ref_kind: "area" },
  { value: 50, label: "Arm area Night",       ref_kind: "area" },
  { value: 51, label: "Arm area Away",        ref_kind: "area" },
  { value: 52, label: "Arm area Vacation",    ref_kind: "area" },
];

export function commandOptionFor(value: number): CommandOption | undefined {
  return COMMAND_OPTIONS.find((c) => c.value === value);
}

// Days bitmask bits (matches omni_pca.programs.Days). Bit 0 is unused.
export const DAY_BITS: ReadonlyArray<{ bit: number; label: string }> = [
  { bit: 0x02, label: "Mon" },
  { bit: 0x04, label: "Tue" },
  { bit: 0x08, label: "Wed" },
  { bit: 0x10, label: "Thu" },
  { bit: 0x20, label: "Fri" },
  { bit: 0x40, label: "Sat" },
  { bit: 0x80, label: "Sun" },
];

// Program type constants (matches omni_pca.programs.ProgramType).
export const PROGRAM_TYPE_TIMED = 1;
export const PROGRAM_TYPE_EVENT = 2;
export const PROGRAM_TYPE_YEARLY = 3;
export const PROGRAM_TYPE_REMARK = 4;

/** HA's hass object — minimal surface we use. */
export interface Hass {
  connection: {
    sendMessagePromise<T>(msg: unknown): Promise<T>;
    subscribeEvents<T>(
      callback: (event: T) => void,
      eventType: string,
    ): Promise<() => Promise<void>>;
  };
  config?: {
    entries?: Record<string, unknown>;
  };
  // Whole hass is much larger; we only touch what we need.
}
