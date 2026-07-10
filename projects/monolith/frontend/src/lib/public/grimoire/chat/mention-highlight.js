// Post-processes renderMarkdown's OUTPUT to turn touched entity names into
// type-colored, clickable links to their World page (the "GROUNDED IN"
// mention treatment on chat replies).
//
// Contract: `html` MUST be freshly rendered, already-escaped markdown output
// (renderMarkdown's return value) with no raw user HTML in it. Call this
// exactly once per render, on fresh input only. Never call it on its own
// return value: the anchor this module inserts becomes a tag segment on a
// second pass, but the text it wraps would still be visible to the text-only
// split and could re-match, double-wrapping the mention. Every call site
// satisfies this by calling renderMarkdown() then highlightMentions() in the
// same expression, never chaining twice.
//
// Security: because the input is already HTML-escaped, titles are matched
// against their escaped form (an entity title containing "&" matches the
// "&amp;" the browser will render), and the only markup this module ever
// inserts is its own literal <a> wrapper. `entity_type` is attacker-
// influenced (it rides in from the corpus) and is interpolated into a CSS
// custom-property name, so it is allow-listed before use. `id` is attacker-
// influenced too and is interpolated into the href; it goes through
// encodeURIComponent and nothing else, the same rule worldHref() applies.

const escapeHtml = (s) =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const TYPE_ALLOWLIST = /^[a-z_]+$/;

/**
 * Wrap touched entity-name mentions in `html` with a type-colored link to
 * that entity's World page.
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
  // and attributes can never be corrupted. Since this module now emits an
  // <a>, also track whether a text segment sits inside an already-open <a>
  // (renderMarkdown's [[wikilink]] anchors) and skip it there: nesting an
  // <a> inside an <a> is invalid HTML that browsers silently mangle by
  // closing the outer tag early, which would break the wikilink's own
  // markup. Both chat call sites pass an empty title map, so a wikilink here
  // always renders as an anchor-shaped "wl dead" span with no href, but the
  // tag is still structurally an <a> and this guard costs nothing to keep.
  let inAnchor = false;
  return html
    .split(/(<[^>]*>)/)
    .map((seg) => {
      if (seg.startsWith("<")) {
        if (/^<a[\s>]/i.test(seg)) inAnchor = true;
        else if (/^<\/a>/i.test(seg)) inAnchor = false;
        return seg;
      }
      if (inAnchor) return seg;
      return seg.replace(pattern, (m) => {
        const lower = m.toLowerCase();
        const hit = escaped.find(
          ({ escapedTitle }) => escapedTitle.toLowerCase() === lower,
        );
        const type = TYPE_ALLOWLIST.test(hit?.e.entity_type ?? "")
          ? hit.e.entity_type
          : "class";
        const href = `/app/grimoire/world?e=${encodeURIComponent(hit.e.id)}`;
        return `<a class="gmark" data-type="${type}" href="${href}" style="color: var(--grim-type-${type}, currentColor)">${m}</a>`;
      });
    })
    .join("");
}
