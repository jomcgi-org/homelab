// Review freshness of a knowledge note, shown beside (never folded into)
// confidence and verification state. A due or unknown fact is history, not
// current context, whatever its confidence says.

const MINUTE = 60 * 1000;

function parse(value) {
  if (typeof value !== "string") return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

function stamp(ms) {
  return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + "Z";
}

// `note` is the private note-detail payload. Returns null when it carries no
// freshness fields (the public endpoint), so the panel renders nothing.
export function describeFreshness(note, nowMs = Date.now()) {
  if (!note || typeof note.freshness !== "string") return null;
  const deadline = parse(note.review_after);
  // The payload was computed when it was fetched; the page may be open later.
  let state = note.freshness;
  if (state === "current" && (deadline === null || nowMs >= deadline)) {
    state = deadline === null ? "unknown" : "due";
  }
  const reviewed = parse(note.last_reviewed_at);
  const observed = parse(note.observed_at);
  const outcome = note.last_review_outcome ?? null;
  const lines = [];
  if (state === "current") {
    const minutes = Math.max(1, Math.ceil((deadline - nowMs) / MINUTE));
    lines.push(`Review by ${stamp(deadline)} (in ${formatSpan(minutes)})`);
  } else if (state === "due") {
    lines.push(`Review was due ${stamp(deadline)}; treat as history`);
  } else {
    lines.push("No trustworthy observation date; treat as history");
  }
  if (observed !== null) lines.push(`Observed ${stamp(observed)}`);
  if (reviewed !== null) lines.push(`Last verified ${stamp(reviewed)}`);
  if (state !== "current" && outcome) {
    lines.push(`Last review ${outcome.status}: ${outcome.reason}`);
  }
  if (note.requires_authoritative_observation) {
    lines.push(
      "Volatile: confirm against an authoritative source before acting",
    );
  }
  return {
    state,
    label: state.toUpperCase(),
    lines,
    volatile: Boolean(note.requires_authoritative_observation),
  };
}

function formatSpan(minutes) {
  if (minutes < 120) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}
