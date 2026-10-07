// Splits rendered post HTML at ```interactive markers (see renderDoc). Each
// marker takes the next N top-level blocks as its fallback: the page renders
// them as-is on the server and swaps in the interactive figure on hydration.
const MARKER =
  /<div class="ix-slot" data-ix="([a-z][a-z0-9-]*)" data-ix-blocks="([1-9])"><\/div>\n?/g;

// End index of the top-level element that starts at or after `from`.
function blockEnd(html, from) {
  const start = html.slice(from).search(/\S/);
  if (start < 0) return -1;
  const at = from + start;
  const open = /^<([a-z][a-z0-9]*)\b/.exec(html.slice(at));
  if (!open) return -1;
  const tag = open[1];
  const re = new RegExp(`<(/?)${tag}\\b[^>]*>`, "g");
  re.lastIndex = at;
  let depth = 0;
  for (let m; (m = re.exec(html));) {
    depth += m[1] ? -1 : 1;
    if (depth === 0) return re.lastIndex;
  }
  return -1;
}

export function splitInteractive(html) {
  const parts = [];
  let previous = 0;
  for (const match of html.matchAll(MARKER)) {
    if (match.index < previous) continue;
    let end = match.index + match[0].length;
    for (let i = 0; i < Number(match[2]); i++) {
      const next = blockEnd(html, end);
      if (next < 0) break;
      end = next;
    }
    if (match.index > previous)
      parts.push({ html: html.slice(previous, match.index) });
    parts.push({
      ix: match[1],
      fallback: html.slice(match.index + match[0].length, end),
    });
    previous = end;
  }
  if (previous < html.length) parts.push({ html: html.slice(previous) });
  return parts;
}
