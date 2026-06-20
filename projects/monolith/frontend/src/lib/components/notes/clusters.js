// Maps Note.type values to CSS custom-property references defined in
// src/lib/styles/shared/tokens.css. Unknown types fall through to
// --cluster-other so new types appear in the legend without a code
// change.

// Hex strings, not CSS-var references — canvas fillStyle can't follow
// var() chains and getPropertyValue() returns the authored "var(--x)"
// rather than the resolved colour in most browsers. Hardcoding here
// keeps the canvas renderer simple and the legend swatches consistent.
//
// Palette matches the prototype's cluster colours; the Note.type → colour
// mapping uses semantics from that aesthetic:
//   atom/fact → yellow (most common, distilled facts)
//   raw       → black  (raw inputs, pre-distillation)
//   gap       → coral  (missing knowledge — visually loud)
//   active    → blue   (in-progress tasks)
//   paper     → green  (curated reference material)
//   other     → white  (catch-all)
export const CLUSTER_COLORS = {
  atom: "#F5D90A",
  fact: "#5DD879",
  raw: "#141414",
  gap: "#FF6B5B",
  active: "#7DB8E8",
  paper: "#5DD879",
};

const FALLBACK = "#FFFFFF";

// Display labels — Note.type values are stored verbatim in the DB but
// "gap" reads as a perceptual negative ("missing knowledge"); the
// vault's `_researching/` directory naming is what the user sees in
// their filesystem, so mirror that here.
export const CLUSTER_LABELS = {
  atom: "atom",
  fact: "fact",
  raw: "raw",
  gap: "researching",
  active: "active",
  paper: "paper",
};

export function labelFor(type) {
  return CLUSTER_LABELS[type] ?? type ?? "other";
}

export function colorFor(type) {
  return CLUSTER_COLORS[type] ?? FALLBACK;
}
