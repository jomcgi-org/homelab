// Post-processes renderMarkdown's OUTPUT to underline touched entity names
// in their type color (the "GROUNDED IN" mention treatment on chat replies).
//
// Contract: `html` MUST be freshly rendered, already-escaped markdown output
// (renderMarkdown's return value) with no raw user HTML in it. Call this
// exactly once per render, on fresh input only. Never call it on its own
// return value: the span this module inserts becomes a tag segment on a
// second pass, but the text it wraps would still be visible to the text-only
// split and could re-match, double-wrapping the mention. Both call sites in
// chat/+page.svelte satisfy this by calling renderMarkdown() then
// highlightMentions() in the same expression, never chaining twice.
//
// Security: because the input is already HTML-escaped, titles are matched
// against their escaped form (an entity title containing "&" matches the
// "&amp;" the browser will render), and the only markup this module ever
// inserts is its own literal <span> wrapper. `entity_type` is attacker-
// influenced (it rides in from the corpus) and is interpolated into a CSS
// custom-property name, so it is allow-listed before use.

const escapeHtml = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const TYPE_ALLOWLIST = /^[a-z_]+$/;

/**
 * Wrap touched entity-name mentions in `html` with a type-colored span.
 *
 * @param {string} html renderMarkdown() output; already HTML-escaped.
 * @param {{id: any, title: string, kind: string, entity_type?: string}[]} touched
 * @returns {string}
 */
export function highlightMentions(html, touched) {
  const entities = (touched ?? [])
    .filter((t) => t?.kind === "entity" && t.title)
    .sort((a, b) => b.title.length - a.title.length);
  if (!entities.length) return html;
  // Sort longer titles first (already done above) and match ALL of them in
  // a single alternation pass. A sequential "replace, then replace again
  // with the next title" approach would re-scan text this function itself
  // just inserted (the previous entity's span content), double-wrapping a
  // shorter title that is a substring of a longer one already matched.
  // Regex alternation tries branches left-to-right, so the longer-first
  // ordering also decides which entity wins an overlapping match.
  const escaped = entities.map((e) => ({
    e,
    escapedTitle: escapeHtml(e.title),
  }));
  const pattern = new RegExp(
    escaped.map(({ escapedTitle }) => escapeRegex(escapedTitle)).join("|"),
    "gi",
  );
  // Split into tag and text segments; only rewrite text segments, so markup
  // and attributes can never be corrupted.
  return html
    .split(/(<[^>]*>)/)
    .map((seg) => {
      if (seg.startsWith("<")) return seg;
      return seg.replace(pattern, (m) => {
        const lower = m.toLowerCase();
        const hit = escaped.find(
          ({ escapedTitle }) => escapedTitle.toLowerCase() === lower,
        );
        const type = TYPE_ALLOWLIST.test(hit?.e.entity_type ?? "")
          ? hit.e.entity_type
          : "class";
        return `<span class="gmark" style="text-decoration-color: var(--grim-type-${type}, currentColor)">${m}</span>`;
      });
    })
    .join("");
}
