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
  /** Raw Program field values; included for compact-form programs so
   *  the editor can seed its form from real data rather than defaults. */
  fields?: ProgramFields;
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


// --------------------------------------------------------------------------
// Event-ID encode/decode for the EVENT-program editor.
//
// Mirrors the Python helpers in omni_pca.program_engine — the 16-bit
// event_id uses different bit patterns per category. Each "category"
// in the UI maps to a different chunk of the ID space.
// --------------------------------------------------------------------------


export type EventCategory =
  | "button"   // USER_MACRO_BUTTON   (evt & 0xFF00) == 0x0000
  | "zone"     // ZONE_STATE_CHANGE   (evt & 0xFC00) == 0x0400
  | "unit"     // UNIT_STATE_CHANGE   (evt & 0xFC00) == 0x0800
  | "fixed"    // hard-coded IDs (phone / AC power)
  | "raw";     // anything else — show numeric

export interface DecodedEvent {
  category: EventCategory;
  /** For "button": 1..255 */
  button?: number;
  /** For "zone": 1..256, plus state 0=secure / 1=not-ready / 2=trouble / 3=tamper */
  zone?: number;
  zoneState?: number;
  /** For "unit": 1..511 plus on bool */
  unit?: number;
  unitOn?: boolean;
  /** For "fixed": the literal event ID. */
  fixedId?: number;
  /** For "raw": the literal event ID we couldn't classify. */
  raw?: number;
}

// Hand-rolled fixed IDs and labels (matches Python EVENT_* constants).
export const FIXED_EVENTS: ReadonlyArray<{ id: number; label: string }> = [
  { id: 768, label: "Phone line dead" },
  { id: 769, label: "Phone ringing" },
  { id: 770, label: "Phone off hook" },
  { id: 771, label: "Phone on hook" },
  { id: 772, label: "AC power lost" },
  { id: 773, label: "AC power restored" },
];

const ZONE_STATE_LABELS = ["secure", "not ready", "trouble", "tamper"];

export function decodeEventId(eventId: number): DecodedEvent {
  // FIXED first — the bit patterns below would otherwise collapse
  // 768..773 into the "zone state change" category since their top
  // bits look the same.
  if (FIXED_EVENTS.some((f) => f.id === eventId)) {
    return { category: "fixed", fixedId: eventId };
  }
  if ((eventId & 0xFF00) === 0x0000) {
    return { category: "button", button: eventId & 0xFF };
  }
  if ((eventId & 0xFC00) === 0x0400) {
    const zs = eventId & 0x03FF;
    return {
      category: "zone",
      zone: Math.floor(zs / 4) + 1,
      zoneState: zs % 4,
    };
  }
  if ((eventId & 0xFC00) === 0x0800) {
    const us = eventId & 0x03FF;
    return {
      category: "unit",
      unit: Math.floor(us / 2) + 1,
      unitOn: (us & 1) === 1,
    };
  }
  return { category: "raw", raw: eventId };
}

export function encodeEventId(ev: DecodedEvent): number {
  switch (ev.category) {
    case "button":
      return (ev.button ?? 1) & 0xFF;
    case "zone": {
      const zone = (ev.zone ?? 1) - 1;
      const state = (ev.zoneState ?? 0) & 0x03;
      return 0x0400 | ((zone * 4 + state) & 0x03FF);
    }
    case "unit": {
      const unit = (ev.unit ?? 1) - 1;
      const on = ev.unitOn ? 1 : 0;
      return 0x0800 | ((unit * 2 + on) & 0x03FF);
    }
    case "fixed":
      return ev.fixedId ?? 768;
    case "raw":
    default:
      return ev.raw ?? 0;
  }
}

export function eventIdFromFields(fields: ProgramFields): number {
  return ((fields.month ?? 0) << 8) | (fields.day ?? 0);
}

export function packEventIdIntoFields(
  fields: ProgramFields, eventId: number,
): ProgramFields {
  return {
    ...fields,
    month: (eventId >> 8) & 0xFF,
    day: eventId & 0xFF,
  };
}

export function zoneStateLabel(state: number): string {
  return ZONE_STATE_LABELS[state] ?? `state ${state}`;
}


// Month abbreviations for the YEARLY editor.
export const MONTH_NAMES = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];


// --------------------------------------------------------------------------
// Compact-form AND-IF condition encode/decode for the inline-conditions
// editor (TIMED/EVENT/YEARLY cond + cond2 fields).
//
// Mirrors clsText.GetConditionalText (clsText.cs:2224-2274) and the
// Python _emit_traditional_cond in program_renderer.py. Bit layout:
//
//   family = (cond >> 8) & 0xFC
//   selector bit = (cond & 0x0200) — meaning depends on family
//
//   family 0x00 OTHER  — cond & 0x0F = enuMiscConditional (NONE=0,
//                                     NEVER=1, LIGHT=2, DARK=3, ...)
//   family 0x04 ZONE   — low 8 bits = zone index; selector bit
//                        0=secure, 1=not ready
//   family 0x08 CTRL   — low 9 bits = unit index; selector bit
//                        0=OFF, 1=ON
//   family 0x0C TIME   — low 8 bits = time-clock index; selector bit
//                        0=disabled, 1=enabled
//   family >= 0x10 SEC — (cond >> 8) & 0x0F = area, (cond >> 12) & 0x07 = mode
//
// cond == 0 means "no condition" (NONE).
// --------------------------------------------------------------------------


export type CondFamily =
  | "none"   // cond = 0 — no inline condition
  | "misc"   // OTHER family (NEVER, LIGHT, DARK, PHONE_*, AC_POWER_*, …)
  | "zone"   // ZONE family — zone + secure/not-ready
  | "unit"   // CTRL family — unit + on/off
  | "time"   // TIME family — time-clock + enabled/disabled
  | "sec";   // SEC family — area + security mode

export interface DecodedCondition {
  family: CondFamily;
  /** misc-conditional index (0..15) — used when family == "misc". */
  misc?: number;
  /** Zone / unit / time-clock / area index — used by the named families. */
  index?: number;
  /** Selector bit: zone "not ready", unit "on", time-clock "enabled". */
  active?: boolean;
  /** SEC family security mode (0..7). */
  mode?: number;
}

// MiscConditional enum (matches omni_pca.programs.MiscConditional).
// Each entry: { value, label }. NONE renders as "always" and NEVER as
// "never" — both common authoring patterns.
export const MISC_CONDITIONALS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0,  label: "always" },
  { value: 1,  label: "never" },
  { value: 2,  label: "it is light outside" },
  { value: 3,  label: "it is dark outside" },
  { value: 4,  label: "phone line is dead" },
  { value: 5,  label: "phone is ringing" },
  { value: 6,  label: "phone is off hook" },
  { value: 7,  label: "phone is on hook" },
  { value: 8,  label: "AC power is off" },
  { value: 9,  label: "AC power is on" },
  { value: 10, label: "battery is low" },
  { value: 11, label: "battery is OK" },
  { value: 12, label: "energy cost is low" },
  { value: 13, label: "energy cost is mid" },
  { value: 14, label: "energy cost is high" },
  { value: 15, label: "energy cost is critical" },
];

// Security modes for the SEC family (matches enuSecurityMode order).
export const SECURITY_MODE_NAMES: ReadonlyArray<{ value: number; label: string }> = [
  { value: 0, label: "Off (disarmed)" },
  { value: 1, label: "Day" },
  { value: 2, label: "Night" },
  { value: 3, label: "Away" },
  { value: 4, label: "Vacation" },
  { value: 5, label: "Day Instant" },
  { value: 6, label: "Night Delayed" },
];

export function decodeCondition(cond: number): DecodedCondition {
  if (cond === 0) return { family: "none" };
  const family = (cond >> 8) & 0xFC;
  const active = (cond & 0x0200) !== 0;
  if (family === 0x00) {
    return { family: "misc", misc: cond & 0x0F };
  }
  if (family === 0x04) {
    return { family: "zone", index: cond & 0xFF, active };
  }
  if (family === 0x08) {
    return { family: "unit", index: cond & 0x01FF, active };
  }
  if (family === 0x0C) {
    return { family: "time", index: cond & 0xFF, active };
  }
  // SEC family (family >= 0x10): area in high nibble of upper byte,
  // mode in top nibble.
  return {
    family: "sec",
    index: (cond >> 8) & 0x0F,
    mode: (cond >> 12) & 0x07,
  };
}

export function encodeCondition(c: DecodedCondition): number {
  switch (c.family) {
    case "none":
      return 0;
    case "misc":
      return (c.misc ?? 0) & 0x0F;  // family 0x00, low nibble = misc
    case "zone": {
      const idx = (c.index ?? 0) & 0xFF;
      return 0x0400 | (c.active ? 0x0200 : 0) | idx;
    }
    case "unit": {
      const idx = (c.index ?? 0) & 0x01FF;
      return 0x0800 | (c.active ? 0x0200 : 0) | idx;
    }
    case "time": {
      const idx = (c.index ?? 0) & 0xFF;
      return 0x0C00 | (c.active ? 0x0200 : 0) | idx;
    }
    case "sec": {
      const area = (c.index ?? 1) & 0x0F;
      const mode = (c.mode ?? 0) & 0x07;
      return (mode << 12) | (area << 8);
    }
  }
}

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
