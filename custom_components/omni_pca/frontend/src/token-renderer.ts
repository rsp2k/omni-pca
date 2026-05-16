// Token-stream → DOM. Each TokenKind gets distinctive styling so the
// structured-English programs read cleanly even at a glance.
//
// REF tokens are rendered as <button> nodes so they can dispatch a
// "ref-click" event for the parent component to act on (filter the
// list, jump to that entity's HA page, etc.).

import { html, TemplateResult } from "lit";
import { Token } from "./types.js";

export function renderTokens(
  tokens: Token[],
  onRefClick?: (kind: string, id: number) => void,
): TemplateResult {
  return html`${tokens.map((t) => renderToken(t, onRefClick))}`;
}

function renderToken(
  t: Token,
  onRefClick?: (kind: string, id: number) => void,
): TemplateResult {
  switch (t.k) {
    case "newline":
      return html`<br />`;
    case "indent":
      // Convert leading spaces to a CSS class so the panel can switch
      // indent styling (e.g. left border) without re-rendering tokens.
      return html`<span class="indent">${t.t}</span>`;
    case "keyword":
      return html`<span class="keyword">${t.t}</span>`;
    case "operator":
      return html`<span class="operator">${t.t}</span>`;
    case "value":
      return html`<span class="value">${t.t}</span>`;
    case "ref": {
      const handler = onRefClick && t.ek && typeof t.ei === "number"
        ? () => onRefClick(t.ek!, t.ei!)
        : undefined;
      return html`<button
        type="button"
        class="ref ref-${t.ek}"
        title=${t.ek ?? ""}
        @click=${handler}
      >
        <span class="ref-name">${t.t}</span>
        ${t.s ? html`<span class="ref-state">${t.s}</span>` : ""}
      </button>`;
    }
    default:
      return html`<span>${t.t}</span>`;
  }
}
