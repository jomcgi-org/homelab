// Pure helpers for the factory board. Everything the page derives from the
// /agents/factory payload lives here so it is testable without mounting
// Svelte, the same split run-format.js gives RunView.
import { computeRanks } from "../dag.js";

export const NODE_STATE_WORD = {
  running: "running",
  done: "done",
  failed: "failed",
  uncertain: "uncertain",
  cancelled: "cancelled",
  retired: "retired",
  pending: "queued",
};

export const RECEIPT_STATE_WORD = {
  queued: "queued",
  admitted: "in flight",
  uncertain: "uncertain",
  succeeded: "landed",
  failed: "failed",
  cancelled: "cancelled",
};

/** Ranks the plan for the DAG strip; nodes keep their board fields. */
export function planRanks(nodes) {
  const keyed = (nodes ?? []).map((node) => ({ ...node, key: node.node_key }));
  return computeRanks(keyed);
}

/** fmtCost renders 0 as an empty string; a ledger wants the zero. */
export function money(value) {
  if (value === null || value === undefined || value === "") return "";
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  return n === 0 ? "$0.00" : `$${n.toFixed(2)}`;
}

/** Share of the task budget already committed, clamped to [0, 1]. */
export function budgetShare(receipt) {
  const budget = Number(receipt?.policy?.task_budget_usd);
  const spent = Number(receipt?.committed_cost_usd);
  if (!(budget > 0) || !(spent >= 0)) return 0;
  return Math.min(1, spent / budget);
}

/** Starts used against the task's derived allowance, as a fraction. */
export function turnShare(receipt) {
  const limit = Number(
    receipt?.allowance?.turns ?? receipt?.policy?.max_task_turns_hard,
  );
  const used = Number(receipt?.turns_used);
  if (!(limit > 0) || !(used >= 0)) return 0;
  return Math.min(1, used / limit);
}

function seconds(value, now) {
  const t = value ? Date.parse(value) : NaN;
  return Number.isFinite(t) ? Math.round((t - now) / 1000) : null;
}

/** "3h 12m left" or "overdue 20m" from the receipt's deadline. */
export function deadlineLabel(receipt, now = Date.now()) {
  const left = seconds(receipt?.deadline_at, now);
  if (left === null) return "";
  const abs = Math.abs(left);
  const h = Math.floor(abs / 3600);
  const m = Math.floor((abs % 3600) / 60);
  const span = h ? `${h}h ${m}m` : `${m}m`;
  return left >= 0 ? `${span} left` : `overdue ${span}`;
}

/** The node the operator most wants to see first: attention, then running. */
export function focusNode(nodes) {
  const list = nodes ?? [];
  return (
    list.find((n) => n.state === "uncertain" || n.state === "failed") ??
    list.find((n) => n.state === "running") ??
    list.find((n) => n.state === "pending") ??
    list.at(-1) ??
    null
  );
}

/** The current phase word for a card: what is running, or the outcome. */
export function phaseLabel(receipt) {
  if (!receipt) return "";
  if (receipt.state === "queued") return "waiting for a slot";
  if (["succeeded", "failed", "cancelled"].includes(receipt.state)) {
    return RECEIPT_STATE_WORD[receipt.state];
  }
  const node = focusNode(receipt.nodes);
  if (!node) return receipt.task_paused ? "paused" : "planning";
  const word = NODE_STATE_WORD[node.state] ?? node.state;
  return receipt.task_paused
    ? `paused · ${node.label}`
    : `${word} · ${node.label}`;
}

/** Counts for the launcher strip. */
export function laneSummary(board) {
  return {
    state: board?.state ?? "unknown",
    active: board?.active?.length ?? 0,
    queued: board?.queued?.length ?? 0,
    paused: (board?.active ?? []).filter((r) => r.task_paused).length,
  };
}

/**
 * The first message for a conductor conversation about one task. The
 * console's Launcher prefills from these query params, so the operator lands
 * in a normal session with the model and the context already in place.
 */
export function conductorPrompt(receipt) {
  if (!receipt) {
    return "Sync with the factory conductor. I want to discuss a new proposal for the factory: ";
  }
  const nodes = (receipt.nodes ?? [])
    .map((n) => `${n.node_key} [${n.state}${n.model ? `, ${n.model}` : ""}]`)
    .join(", ");
  const lines = [
    `Sync with the factory conductor about task ${receipt.task_id} (#${receipt.issue_number}: ${receipt.title}).`,
    `Receipt state: ${receipt.state}${receipt.task_paused ? " (paused)" : ""}.`,
    nodes ? `Current plan: ${nodes}.` : "No plan nodes yet.",
    "Read the task's factory artifacts before answering. I want to discuss: ",
  ];
  return lines.join("\n");
}

/**
 * The conductor is whatever the policy says plans: the receipt's own policy
 * for a task, the board's for a fresh proposal, astra when neither is loaded.
 */
export function conductorModel(receipt, board) {
  return (
    receipt?.policy?.conductor_model ||
    board?.policy?.conductor_model ||
    "astra"
  );
}

export function conductorHref(receipt, board = null) {
  const params = new URLSearchParams();
  params.set("compose", "1");
  params.set("model", conductorModel(receipt, board));
  params.set("prompt", conductorPrompt(receipt));
  return `/agents?${params.toString()}`;
}
