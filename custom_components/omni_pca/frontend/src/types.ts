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
