import { RUN_LEXICON } from "./run-lexicon.js";

export const relSeconds = (a, b) =>
  Math.max(0, Math.round((Date.parse(b) - Date.parse(a)) / 1000));

export function fmtDur(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
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
  const n = Number(value || 0);
  if (!(n > 0)) return "";
  return n >= 0.01 ? `$${n.toFixed(2)}` : `$${n.toFixed(4)}`;
}

export function ordinal(n) {
  return RUN_LEXICON.ordinals[String(n)] || `${n}${RUN_LEXICON.ordinals.other}`;
}
