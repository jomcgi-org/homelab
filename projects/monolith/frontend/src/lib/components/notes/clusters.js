// The private graph retains its legacy palette. Public sheet consumers opt
// into semantic page tokens; canvas-palette.js resolves those through CSS.
export const CLUSTER_COLORS = {
  atom: "#F5D90A",
  fact: "#5DD879",
  raw: "#141414",
  gap: "#FF6B5B",
  active: "#7DB8E8",
  paper: "#5DD879",
};

const FALLBACK = "#FFFFFF";

export const SHEET_CLUSTER_COLORS = {
  atom: "var(--warn)",
  fact: "var(--tone-cache)",
  raw: "var(--ink)",
  gap: "var(--tone-disk)",
  active: "var(--tone-gpu)",
  paper: "var(--tone-ram)",
};

// Display labels — Note.type values are stored verbatim in the DB but
// "gap" reads as a perceptual negative ("missing knowledge"); the
// vault's `_researching/` directory naming is what the user sees in
// their filesystem, so mirror that here.
const CLUSTER_LABELS = {
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

export function colorFor(type, sheet = false) {
  if (sheet) return SHEET_CLUSTER_COLORS[type] ?? "var(--sheet)";
  return CLUSTER_COLORS[type] ?? FALLBACK;
}
