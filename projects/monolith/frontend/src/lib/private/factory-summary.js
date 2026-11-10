// The dashboard's view of the factory board: what is waiting on Joe, what is
// moving, and the one link into a conductor conversation. Computed on the
// server from /api/agents/factory and refreshed with the rest of the
// dashboard, so the root page never carries the whole board.
import {
  conductorHref,
  conductorModel,
  laneSummary,
} from "../../routes/private/factory/factory-view.js";

const optionFields = (option) => ({
  key: option.key,
  label: option.label ?? option.key,
  effect: option.effect ?? null,
  closes: option.closes ?? [],
  in_favour_of: option.in_favour_of ?? null,
  children: option.children ?? 0,
});

/**
 * The open escalations, with what a phone card needs to answer one in place:
 * the question, the brief's options (recommendation first), the fixed escape
 * options and the exact decision identity a reply must carry.
 */
export function waitingDecisions(board) {
  return (board?.escalations ?? [])
    .filter((item) => item?.open)
    .map((item) => ({
      receipt_id: item.receipt_id ?? null,
      decision_id: item.decision_id ?? null,
      issue_number: item.issue_number ?? null,
      work_item_id: item.work_item_id ?? null,
      kind: item.kind ?? null,
      briefing: Boolean(item.briefing),
      title: item.title ?? "",
      question: item.question ?? "",
      summary: item.summary ?? "",
      recommendation: item.recommendation ?? null,
      url: item.url ?? null,
      pr_url: item.pr_url ?? null,
      options: (item.options ?? []).filter((o) => o?.key).map(optionFields),
      escape: (item.escape ?? []).filter((o) => o?.key).map(optionFields),
    }));
}

/**
 * Null when the board is unavailable, so the page can say so rather than
 * read an empty lane as a quiet one.
 */
export function summarizeBoard(board) {
  if (!board || typeof board !== "object") return null;
  const lanes = laneSummary(board);
  return {
    ok: board.ok !== false,
    // The board's reasons are codes (not_initialized); the readings row
    // prints words.
    reason:
      board.ok === false
        ? String(board.reason || "not ready").replaceAll("_", " ")
        : null,
    state: lanes.state,
    inFlight: lanes.active,
    queued: lanes.queued,
    paused: lanes.paused,
    waiting: waitingDecisions(board),
    conductor: {
      href: conductorHref(null, board),
      model: conductorModel(null, board),
    },
  };
}

/** Validate the complete bounded backend reading before presenting counts. */
export function summarizeFactoryReading(reading) {
  if (!reading || typeof reading !== "object") return null;
  if (reading.ok === false) return summarizeBoard(reading);
  if (
    reading.ok !== true ||
    !Array.isArray(reading.escalations) ||
    ![reading.active_count, reading.queued_count, reading.paused_count].every(
      (n) => Number.isSafeInteger(n) && n >= 0,
    ) ||
    reading.paused_count > reading.active_count
  )
    return null;
  return {
    ...summarizeBoard({ ...reading, active: [], queued: [] }),
    inFlight: reading.active_count,
    queued: reading.queued_count,
    paused: reading.paused_count,
  };
}
