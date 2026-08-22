import { RUN_LEXICON } from "./run-lexicon.js";

export const IN_FLIGHT_STATUSES = new Set(["PENDING", "ENQUEUED"]);

export function isInFlight(run) {
  return IN_FLIGHT_STATUSES.has(run?.dbos_status);
}

export function partitionRuns(runs) {
  return (runs ?? []).reduce(
    (result, run) => {
      result[isInFlight(run) ? "inFlight" : "terminal"].push(run);
      return result;
    },
    { inFlight: [], terminal: [] },
  );
}

export function runActivityAt(run) {
  return run?.completed_at ?? run?.updated_at ?? run?.created_at;
}

function timestamp(value) {
  if (!value) return "";
  return /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : `${value}Z`;
}

export function relativeTime(value, now = Date.now()) {
  if (!value) return RUN_LEXICON.labels.relativeNever;
  const then = Date.parse(timestamp(value));
  if (Number.isNaN(then)) return RUN_LEXICON.labels.relativeUnknown;
  const seconds = Math.max(0, Math.floor((now - then) / 1000));
  if (seconds < 60) return RUN_LEXICON.labels.relativeNow;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}${RUN_LEXICON.units.m}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}${RUN_LEXICON.units.h}`;
  const days = Math.floor(hours / 24);
  if (days === 1) return RUN_LEXICON.labels.relativeYesterday;
  if (days < 30) return `${days}${RUN_LEXICON.units.d}`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}${RUN_LEXICON.units.mo}`;
  return `${Math.floor(months / 12)}${RUN_LEXICON.units.y}`;
}

export function clockTime(value) {
  if (!value) return "";
  const date = new Date(timestamp(value));
  if (Number.isNaN(date.getTime())) return "";
  return `${String(date.getHours()).padStart(2, "0")}:${String(
    date.getMinutes(),
  ).padStart(2, "0")}`;
}

export function recentRuns(
  runs,
  now = Date.now(),
  windowMs = 24 * 60 * 60 * 1000,
) {
  return (runs ?? []).filter((run) => {
    const at = Date.parse(runActivityAt(run) ?? "");
    return !Number.isNaN(at) && now - at < windowMs;
  });
}
