// Keep the author's rendered explanations with each interactive figure.
// Unrecognised figures remain ordinary HTML, including their fallback keys.
const diagrams = new Map([
  ["Where are the weights?", "memory"],
  ["Prefill chunk decisions", "prefill"],
  ["One decode step through one expert layer", "decode"],
  ["How one hot-set slot changes hands without a stall", "swap"],
]);

export function splitSystemDiagrams(html) {
  const parts = [];
  const pattern =
    /<figure class="fig"><figcaption>([^<]+)<\/figcaption>[\s\S]*?<\/figure>\s*<table class="fig-key">[\s\S]*?<\/table>/g;
  let previous = 0;
  for (const match of html.matchAll(pattern)) {
    const mode = diagrams.get(match[1]);
    if (!mode) continue;
    const notes = [
      ...match[0].matchAll(
        /<tr><td[^>]*><span class="co">([^<]+)<\/span><\/td><td>([\s\S]*?)<\/td><\/tr>/g,
      ),
    ].map((row) => ({ key: row[1], html: row[2] }));
    if (!notes.length) continue;
    if (match.index > previous)
      parts.push({ html: html.slice(previous, match.index) });
    parts.push({ diagram: mode, title: match[1], notes });
    previous = match.index + match[0].length;
  }
  if (previous < html.length) parts.push({ html: html.slice(previous) });
  return parts;
}
