import { RUN_LEXICON } from "./run-lexicon.js";

// A console for a durable workflow engine has to keep three things apart that
// these formatters used to collapse into one: a measured zero, a value not
// observed yet, and a value that failed to parse. Only the first is a fact.
// Absent input returns null, which renders as nothing, so no clause can print
// a number nobody measured.

export const relSeconds = (a, b) => {
  const from = Date.parse(a);
  const to = Date.parse(b);
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  return Math.max(0, Math.round((to - from) / 1000));
};

export function fmtDur(seconds) {
  const n = Number(seconds);
  // `Number(seconds) || 0` was the laundering step: an unparseable timestamp
  // became NaN upstream and printed here as "0s ago", a fabricated "just now"
  // sitting beside a real elapsed time.
  if (seconds == null || !Number.isFinite(n)) return null;
  const s = Math.max(0, n);
  if (s < 60) return `${s}${RUN_LEXICON.units.s}`;
  if (s < 3600) return `${Math.floor(s / 60)}${RUN_LEXICON.units.m}`;
  if (s < 172800) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return m
      ? `${h}${RUN_LEXICON.units.h} ${m}${RUN_LEXICON.units.m}`
      : `${h}${RUN_LEXICON.units.h}`;
  }
  return `${Math.floor(s / 86400)}${RUN_LEXICON.units.d}`;
}

export function fmtCost(value) {
  // null means "no figure to show", the empty string means "measured, and it
  // is zero". Callers that need a sentence operand must treat them
  // differently: a missing spend omits its clause, a zero spend says so. The
  // observed "of $50.00 budget" fragment came from having only one falsy
  // answer for both.
  const n = Number(value);
  if (value == null || !Number.isFinite(n)) return null;
  if (!(n > 0)) return "";
  return n >= 0.01 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

export function ordinal(n) {
  return RUN_LEXICON.ordinals[String(n)] || `${n}${RUN_LEXICON.ordinals.other}`;
}
