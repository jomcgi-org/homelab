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

// Asks are keyed by ledger row so two gates about the same ref can coexist.
export function askKey(rowId) {
  return `ask:${rowId}`;
}

export function applyLedgerRows(stage, rows) {
  const next = cloneStage(stage);
  const ordered = [...(Array.isArray(rows) ? rows : [])].sort(
    (a, b) => Number(a.id) - Number(b.id),
  );
  for (const row of ordered) applyRow(next, row);
  return next;
}

export function dismissCard(stage, key) {
  const next = cloneStage(stage);
  const card = next.cards.find((existing) => existing.key === key);
  next.cards = next.cards.filter((existing) => existing.key !== key);
  next.userDismissed[key] = card?.rowId ?? true;
  return next;
}

export function togglePinned(stage, key) {
  const next = cloneStage(stage);
  next.cards = next.cards.map((card) =>
    card.key === key ? { ...card, pinned: !card.pinned } : card,
  );
  return next;
}

export function answerCard(stage, key) {
  const next = cloneStage(stage);
  next.cards = next.cards.map((card) =>
    card.key === key && card.kind === "ask"
      ? { ...card, answered: true }
      : card,
  );
  return next;
}

export function exchangeCount(card, rows) {
  return (Array.isArray(rows) ? rows : []).filter(
    (row) =>
      Number(row.id) > Number(card?.rowId) &&
      (row.call === "show" || row.call === "ask"),
  ).length;
}

export function cardPhase(card, rows) {
  if (card?.pinned) return "front";
  const later = exchangeCount(card, rows);
  if (card?.kind === "tool") return later >= 2 ? "gone" : "front";
  if (later >= 2) return "gone";
  return later === 1 ? "receded" : "front";
}

export function renderSummoningCall(value) {
  const row = value?.row ?? value;
  const payload = payloadOf(row);
  if (row?.call === "attach") {
    return `attach(${row.session_id ?? payload.session_id ?? ""})`;
  }
  if (row?.call === "show") {
    const focus = payload.focus ? `, focus=${payload.focus}` : "";
    return `show(${payload.surface ?? ""}, ${payload.ref ?? ""}${focus})`;
  }
  if (row?.call === "ask") return `ask(${payload.ref ?? ""})`;
  if (row?.call === "dismiss") {
    return payload.surface ? `dismiss(${payload.surface})` : "dismiss()";
  }
  return `${row?.call ?? "tool"}()`;
}

export function renderWireCall(row) {
  const payload = payloadOf(row);
  if (row?.call === "ask") {
    const options = Array.isArray(payload.options)
      ? payload.options.map(String).join(", ")
      : "";
    return `ask(${payload.ref ?? ""}, [${options}])`;
  }
  return renderSummoningCall(row);
}

function cloneStage(stage) {
  return {
    attachedSessionId: stage?.attachedSessionId ?? null,
    cards: (stage?.cards ?? []).map((card) => ({
      ...card,
      payload: { ...(card.payload ?? {}) },
      row: { ...(card.row ?? {}), payload: { ...(card.row?.payload ?? {}) } },
    })),
    userDismissed: { ...(stage?.userDismissed ?? {}) },
  };
}

function payloadOf(row) {
  const payload = row?.payload;
  return payload && typeof payload === "object" ? payload : {};
}

function cardFrom(row, values) {
  return {
    ...values,
    rowId: row.id,
    call: row.call,
    createdAt: row.created_at ?? null,
    payload: { ...payloadOf(row) },
    row: { ...row, payload: { ...payloadOf(row) } },
    pinned: false,
    answered: false,
  };
}

function applyRow(stage, row) {
  const call = row?.call;
  const payload = payloadOf(row);
  if (call === "attach") {
    // The contract puts the binding on the row. Accept the legacy payload too
    // so a persisted companion can replay rows written by the earlier UI.
    const sessionId = row?.session_id ?? payload.session_id;
    if (sessionId != null) stage.attachedSessionId = sessionId;
  } else if (call === "show") {
    applyShow(stage, row, payload);
  } else if (call === "ask") {
    applyAsk(stage, row, payload);
  } else if (call === "dismiss") {
    applyDismiss(stage, payload);
  } else if (!KNOWN_CALLS.has(call) && call) {
    // No writer emits non-voice_ui rows yet (voice_ui.py records four calls); this is the reader half of ADR 058 auto-surfacing.
    upsertCard(
      stage,
      cardFrom(row, {
        key: `tool:${row.id}`,
        kind: "tool",
        surface: null,
        ref: String(payload.ref ?? row.id),
        focus: null,
        question: null,
        options: null,
      }),
    );
  }
}

function applyShow(stage, row, payload) {
  const surface = payload.surface;
  const ref = payload.ref;
  if (!KNOWN_SURFACES.has(surface) || ref == null || ref === "") return;
  const key = surfaceKey(surface, ref);
  // Surface dismissal is sticky until a newer row re-shows this surface:ref.
  const dismissedAt = stage.userDismissed[key];
  if (dismissedAt != null) {
    const dismissedId = Number(dismissedAt);
    if (
      dismissedAt === true ||
      !Number.isFinite(dismissedId) ||
      Number(row.id) <= dismissedId
    ) {
      return;
    }
    delete stage.userDismissed[key];
  }
  upsertCard(
    stage,
    cardFrom(row, {
      key,
      kind: "surface",
      surface,
      ref: String(ref),
      focus: payload.focus ?? null,
      question: null,
      options: null,
    }),
  );
}

function applyAsk(stage, row, payload) {
  const ref = payload.ref;
  if (ref == null || ref === "") return;
  const key = askKey(row.id);
  if (stage.userDismissed[key]) return;
  upsertCard(
    stage,
    cardFrom(row, {
      key,
      kind: "ask",
      surface: null,
      ref: String(ref),
      focus: null,
      question: payload.question == null ? "" : String(payload.question),
      options: Array.isArray(payload.options)
        ? payload.options.map(String)
        : [],
    }),
  );
}

function applyDismiss(stage, payload) {
  const surface = payload.surface;
  stage.cards = stage.cards.filter(
    (card) =>
      card.pinned ||
      (surface != null && surface !== "" && card.surface !== surface),
  );
}

function upsertCard(stage, card) {
  const index = stage.cards.findIndex((existing) => existing.key === card.key);
  if (index >= 0) {
    if (stage.cards[index].pinned) return;
    stage.cards.splice(index, 1);
    stage.cards.unshift(card);
  } else {
    stage.cards.unshift(card);
  }
}
