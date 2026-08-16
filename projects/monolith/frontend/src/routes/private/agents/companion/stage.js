const KNOWN_CALLS = new Set(["attach", "show", "ask", "dismiss"]);
const KNOWN_SURFACES = new Set(["run", "walkthrough", "transcript", "vm"]);

export function emptyStage() {
  return {
    attachedSessionId: null,
    cards: [],
    userDismissed: {},
  };
}

export function surfaceKey(surface, ref) {
  return `${surface}:${ref}`;
}

// Asks are keyed by their ledger row, not by `ref`. Two gates can be raised
// about the same subject ("merge?" then "the suite is red, still merge?"), and
// keying on ref would make the second silently replace the first. It would also
// mean dismissing one gate permanently suppressed every later gate on that ref.
export function askKey(rowId) {
  return `ask:${rowId}`;
}

export function applyLedgerRows(stage, rows) {
  const next = cloneStage(stage);
  const batch = Array.isArray(rows) ? rows : [];
  if (batch.length === 0) return next;
  const ordered = [...batch].sort((a, b) => Number(a.id) - Number(b.id));
  for (const row of ordered) applyRow(next, row);
  return next;
}

export function dismissCard(stage, key) {
  const next = cloneStage(stage);
  next.cards = next.cards.filter((card) => card.key !== key);
  next.userDismissed[key] = true;
  return next;
}

function cloneStage(stage) {
  return {
    attachedSessionId: stage?.attachedSessionId ?? null,
    cards: (stage?.cards ?? []).map((card) => ({ ...card })),
    userDismissed: { ...(stage?.userDismissed ?? {}) },
  };
}

function payloadOf(row) {
  const payload = row?.payload;
  return payload && typeof payload === "object" ? payload : {};
}

function applyRow(stage, row) {
  const call = row?.call;
  if (!KNOWN_CALLS.has(call)) return;
  const payload = payloadOf(row);
  if (call === "attach") {
    if (payload.session_id != null)
      stage.attachedSessionId = payload.session_id;
    return;
  }
  if (call === "show") {
    applyShow(stage, row, payload);
    return;
  }
  if (call === "ask") {
    applyAsk(stage, row, payload);
    return;
  }
  applyDismiss(stage, payload);
}

function applyShow(stage, row, payload) {
  const surface = payload.surface;
  const ref = payload.ref;
  if (!KNOWN_SURFACES.has(surface) || ref == null || ref === "") return;
  const key = surfaceKey(surface, ref);
  delete stage.userDismissed[key];
  upsertCard(stage, {
    key,
    kind: "surface",
    surface,
    ref: String(ref),
    focus: payload.focus ?? null,
    question: null,
    options: null,
    rowId: row.id,
    call: "show",
  });
}

function applyAsk(stage, row, payload) {
  const ref = payload.ref;
  if (ref == null || ref === "") return;
  const key = askKey(row.id);
  if (stage.userDismissed[key]) return;
  const options = Array.isArray(payload.options)
    ? payload.options.map(String)
    : [];
  upsertCard(stage, {
    key,
    kind: "ask",
    surface: null,
    ref: String(ref),
    focus: null,
    question: payload.question == null ? "" : String(payload.question),
    options,
    rowId: row.id,
    call: "ask",
  });
}

function applyDismiss(stage, payload) {
  const surface = payload.surface;
  if (surface == null || surface === "") {
    stage.cards = [];
    return;
  }
  stage.cards = stage.cards.filter((card) => card.surface !== surface);
}

function upsertCard(stage, card) {
  const index = stage.cards.findIndex((existing) => existing.key === card.key);
  if (index >= 0) stage.cards[index] = card;
  else stage.cards.push(card);
}
