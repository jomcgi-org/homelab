import { RUN_LEXICON } from "./run-lexicon.js";

export function firstLine(value) {
  return String(value ?? "")
    .trim()
    .split("\n")[0];
}

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

// ---------------------------------------------------------------------------
// Phrases
//
// Every joined line is built here rather than assembled in markup, because
// markup cannot hold spacing reliably. Svelte trims whitespace at block
// boundaries, so a separator written as the first text inside an {#if} loses
// the space before it, and a line break inside a tag produces no text node at
// all. Both were observed live: "projects/monolith/local·why" and
// "... | head -405s ago". Those read as copy bugs but are formatting
// artifacts, invisible in review and reintroducible by prettier on any edit.
//
// A phrase owns its own separators and decides what to do when an operand is
// absent: omit the clause, never emit half of one. Templates then render one
// string per line and never place a separator next to a block tag.
//
// These live here rather than in run-lexicon.js as the review suggested,
// because run-format.js already imports the lexicon and the reverse direction
// would be an import cycle. The lexicon keeps the words; this keeps the joins.
// ---------------------------------------------------------------------------

const SEP = ` ${RUN_LEXICON.punct.dot} `;

/** Join meta parts with the separator, dropping absent and empty ones. */
export function joinMeta(...parts) {
  const kept = parts.filter((part) => part != null && part !== "");
  return kept.length ? kept.join(SEP) : null;
}

/**
 * Spend, always whole or absent.
 *
 * fmtCost distinguishes a measured zero ("") from no figure at all (null), so
 * a run that has not spent anything says so instead of leaving the observed
 * headless fragment "of $50.00 budget".
 */
export function spendOfBudget(cost, budget) {
  const spent = fmtCost(cost);
  const cap = fmtCost(budget);
  if (!cap) return spent || null;
  if (spent === "") return RUN_LEXICON.labels.noSpendYet;
  if (!spent) return null;
  return `${spent} ${RUN_LEXICON.labels.of} ${cap} ${RUN_LEXICON.labels.budgetWord}`;
}

/** "2m ago", or nothing when the elapsed time was never measured. */
export function agoPhrase(seconds) {
  const duration = fmtDur(seconds);
  return duration ? `${duration} ${RUN_LEXICON.labels.ago}` : null;
}

/** "started 2m ago", or nothing. */
export function startedAgo(seconds) {
  const phrase = agoPhrase(seconds);
  return phrase ? `${RUN_LEXICON.labels.started} ${phrase}` : null;
}

/** "running 2m" when the duration is known, bare state word when it is not. */
export function stateFor(word, seconds) {
  const duration = fmtDur(seconds);
  if (!word) return duration;
  return duration ? `${word} ${duration}` : word;
}

/** "attempt 2 · running 20m · $0.12", omitting whichever parts are absent. */
export function attemptMeta(n, word, seconds, cost) {
  return joinMeta(
    `${RUN_LEXICON.labels.attempt} ${n}`,
    stateFor(word, seconds),
    fmtCost(cost),
  );
}

/** "2nd in line". */
export function queuePosition(position) {
  return `${ordinal(position)} ${RUN_LEXICON.labels.positionWord}`;
}

/** "2nd on the codex queue". */
export function queuedOnQueue(position, name) {
  return `${ordinal(position)} ${RUN_LEXICON.labels.queuedOn} ${name} ${RUN_LEXICON.labels.queueWord}`;
}

/**
 * "engine: unreachable, showing 2m old state".
 *
 * Drops to the claim alone when the snapshot age was never measured, rather
 * than asserting the state is "0s old", which would read as fresh.
 */
export function engineStale(seconds) {
  const head = `${RUN_LEXICON.labels.engine}${RUN_LEXICON.punct.colon}`;
  const age = fmtDur(seconds);
  // staleShowing trails off into an age, so without one it would leave the
  // sentence hanging: exactly the fragment this layer exists to prevent.
  return age
    ? `${head} ${RUN_LEXICON.labels.staleShowing} ${age} ${RUN_LEXICON.labels.staleOld}`
    : `${head} ${RUN_LEXICON.labels.staleUnreachable}`;
}
