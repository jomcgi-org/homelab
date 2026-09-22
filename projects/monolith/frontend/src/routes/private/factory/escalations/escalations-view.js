// Pure helpers for the escalations page. Everything the page derives from the
// board's escalation list lives here so it is testable without mounting
// Svelte, the same split factory-view.js gives the factory board.

export const EFFECT_WORD = {
  "agent-ready": "deliver",
  close: "close",
  supersede: "supersede",
  split: "split",
  defer: "defer",
  hold: "hold",
  "escape-close": "close",
  "escape-defer": "defer",
  "escape-dismiss": "dismiss",
};

/** The numbered hotkeys, in the order the buttons render. */
export const HOTKEYS = ["1", "2", "3", "4"];

/**
 * The key each escape action answers to. Letters and Escape rather than more
 * numbers, so the brief's own options keep 1 to 4 whatever the server appends
 * beneath them.
 */
export const ESCAPE_HOTKEYS = {
  "escape:close": "x",
  "escape:defer": "d",
  "escape:dismiss": "Escape",
};

/** How a key name reads on a button, where the event name is not the label. */
export const ESCAPE_KEY_LABEL = { Escape: "Esc" };

function hasValue(value) {
  return value !== null && value !== undefined && value !== "";
}

function evidence(label, value, href = null) {
  if (!label || !hasValue(value)) return null;
  if (href && typeof href !== "string") return null;
  if (href && !href.startsWith("/") && !href.startsWith("https://"))
    return null;
  return { label, value, href };
}

function evidenceList(entries) {
  return entries.filter(Boolean);
}

function workItemReference(item) {
  if (!item) return null;
  const issueNumber = item.issue_number ?? item.github_issue_number;
  const value =
    issueNumber !== null && issueNumber !== undefined
      ? `#${issueNumber}${item.title ? ` ${item.title}` : ""}`
      : item.id !== null && item.id !== undefined
        ? `work item ${item.id}`
        : null;
  const href =
    item.id !== null && item.id !== undefined
      ? `/factory/work-items/${item.id}`
      : null;
  return value && href ? { value, href } : null;
}

function workItemEvidence(label, item) {
  const reference = workItemReference(item);
  return reference ? evidence(label, reference.value, reference.href) : null;
}

function workItemEvidenceList(label, items) {
  return (items ?? []).map((item) => workItemEvidence(label, item));
}

/** The five context claims and their compact evidence for one escalation. */
export function contextRows(document) {
  const ask = document?.ask ?? {};
  const stopped = document?.stopped;
  const happened = document?.happened;
  const cost = document?.cost;
  const lineage = document?.lineage;
  const issue =
    ask.issue_number !== null && ask.issue_number !== undefined
      ? `#${ask.issue_number}`
      : ask.work_item_id !== null && ask.work_item_id !== undefined
        ? `work item ${ask.work_item_id}`
        : null;
  const review = happened?.last_review;
  const reviewValue = review
    ? `${review.verdict}${review.summary ? `: ${review.summary}` : ""}`
    : null;
  const pr = happened?.pr;
  const prValue = pr
    ? `${pr.number !== null && pr.number !== undefined ? `#${pr.number}` : "pull request"}${pr.state ? ` ${pr.state}` : ""}`
    : null;

  return [
    {
      key: "ask",
      line: ask.line ?? "no ask recorded",
      evidence: evidenceList([
        evidence("issue", issue, ask.url ?? null),
        evidence("class", ask.task_class),
        evidence("generation", ask.generation),
        evidence("body", ask.body_head?.slice(0, 400)),
      ]),
    },
    {
      key: "stopped",
      line: stopped?.line ?? "no escalation",
      evidence: evidenceList([
        evidence("kind", stopped?.kind),
        evidence("recommendation", stopped?.recommendation),
        evidence("question", stopped?.question),
        evidence("summary", stopped?.summary),
        evidence("reason", stopped?.reason),
      ]),
    },
    {
      key: "happened",
      line: happened?.line ?? "no attempts yet",
      evidence: evidenceList([
        evidence("attempts", happened?.attempts),
        evidence("last review", reviewValue),
        evidence("pr", prValue, pr?.url ?? null),
        evidence("branch", happened?.branch),
      ]),
    },
    {
      key: "cost",
      line: cost?.line ?? "no task cost",
      evidence: evidenceList([
        evidence(
          "committed",
          typeof cost?.committed === "number"
            ? `$${cost.committed.toFixed(2)}`
            : null,
        ),
        evidence(
          "ceiling",
          typeof cost?.ceiling === "number"
            ? `$${cost.ceiling.toFixed(2)}`
            : null,
        ),
        evidence("tasks", cost?.num_tasks),
      ]),
    },
    {
      key: "lineage",
      line: lineage?.line ?? "no work item lineage",
      evidence: evidenceList([
        workItemEvidence("parent", lineage?.parent),
        ...workItemEvidenceList("child", lineage?.children),
        ...workItemEvidenceList("blocked by", lineage?.blocked_by),
        ...workItemEvidenceList("blocks", lineage?.blocks),
        ...(Array.isArray(lineage?.superseded_by)
          ? lineage.superseded_by.map((item) =>
              workItemEvidence("superseded by", item),
            )
          : [workItemEvidence("superseded by", lineage?.superseded_by)]),
        ...workItemEvidenceList("supersedes", lineage?.supersedes),
        ...(lineage?.prior_receipts ?? []).map((receipt) =>
          evidence(
            "prior receipt",
            `receipt ${receipt.id} gen ${receipt.generation} ${receipt.task_class} ${receipt.state}`,
          ),
        ),
      ]),
    },
  ];
}

/** A context cache identity that changes when a new decision replaces one. */
export function contextKey(item) {
  return item.receipt_id + ":" + (item.decision_id ?? "");
}

export function open(escalations) {
  return (escalations ?? []).filter((item) => item.open);
}

export function resolved(escalations) {
  return (escalations ?? []).filter((item) => !item.open);
}

/** What a resolved escalation reads as in the collapsed list. */
export function resolutionLine(item) {
  const decision = item?.resolved;
  if (!decision) return "";
  const who = decision.actor ? ` by ${decision.actor}` : "";
  return `${decision.label ?? decision.option_key}${who}`;
}

/**
 * What each effect does on a refine escalation, where the card is a briefing
 * asking what the work should be and no task is waiting on the answer.
 */
const ADVISORY_EFFECT_LINE = {
  "agent-ready": "labels it agent-ready for the delivery lane",
  close: "closes the issue with a reason",
  defer: "moves it to needs-thought with a wait condition",
  hold: "leaves the issue exactly as it is",
  "escape-close": "closes it as not planned and drops needs-human",
  "escape-defer": "swaps needs-human for needs-thought",
  "escape-dismiss": "clears the card, the issue keeps needs-human",
};

/**
 * And on a delivery escalation, where a task escalated out of the lane with a
 * branch and usually a pull request behind it. Everything except continue and
 * dismiss ends that work, and the card has to say so: the same button that
 * only relabels an issue on a refine card throws away an attempt here.
 */
const DELIVERY_EFFECT_LINE = {
  "agent-ready": "re-admits the work with your answer as its direction",
  close: "closes the issue, cancels the task, abandons the branch",
  defer: "moves it to needs-thought, cancels the task, abandons the branch",
  hold: "leaves the issue as it is, cancels the task, abandons the branch",
  "escape-close":
    "closes it as not planned, cancels the task, abandons the branch",
  "escape-defer":
    "swaps needs-human for needs-thought, cancels the task, abandons the branch",
  "escape-dismiss":
    "clears the card, the task stays escalated for a later answer",
};

/**
 * The effect of one option said in a few words, for the line under a button.
 * A split says how many issues it opens, because that is the part a person
 * cannot see from the label and would otherwise have to guess.
 *
 * ``kind`` is the escalation's own, because the same effect means two
 * different things: on a refine card close is a triage decision, and on a
 * delivery card it also cancels a task and abandons the branch it was on.
 */
export function effectLine(option, kind) {
  if (!option) return "";
  const delivery = kind === "delivery";
  if (option.effect === "supersede") {
    const closes = (option.closes ?? [])
      .map((number) => `#${number}`)
      .join(", ");
    return `Closes ${closes} as superseded by #${option.in_favour_of}`;
  }
  if (option.effect === "split") {
    const n = option.children ?? 0;
    const opens = n === 1 ? "opens 1 issue" : `opens ${n} issues`;
    return delivery
      ? `${opens}, closes this one, cancels the task`
      : `${opens}, closes this one`;
  }
  const lines = delivery ? DELIVERY_EFFECT_LINE : ADVISORY_EFFECT_LINE;
  return lines[option.effect] ?? option.effect;
}

/** The escape options a card offers. Empty once the escalation is resolved. */
export function escapesFor(item) {
  return item?.escape ?? [];
}

/** The escape option a key fires on this card, or null when none does. */
export function escapeForKey(item, key) {
  return (
    escapesFor(item).find((option) => ESCAPE_HOTKEYS[option.key] === key) ??
    null
  );
}

/** What the hotkey reads as on an escape button. */
export function escapeHotkey(option) {
  const key = ESCAPE_HOTKEYS[option?.key];
  if (!key) return "";
  return ESCAPE_KEY_LABEL[key] ?? key;
}

/**
 * Whether an escape is asked about before it is sent. Only the close is: it
 * is the one action on the card that another button cannot undo.
 */
export function needsConfirm(option) {
  return option?.key === "escape:close";
}

/** The one line a close confirmation puts in front of the operator. */
export function confirmLine(item) {
  return `Close #${item?.issue_number} as not planned and drop needs-human? Press x again, or Esc to cancel.`;
}

/** Clamp an index into a list, returning -1 for an empty one. */
export function clampIndex(index, length) {
  if (!length) return -1;
  return Math.min(Math.max(index, 0), length - 1);
}

/**
 * Where j and k move the cursor. Bounded rather than wrapping: a wrap on a
 * list you are working down reads as losing your place.
 */
export function moveCursor(index, delta, length) {
  if (!length) return -1;
  return clampIndex((index < 0 ? 0 : index) + delta, length);
}

/**
 * The option a number key picks, or null when that key names nothing. Keys
 * are positional because the recommendation is always first, so 1 is always
 * "do what was recommended".
 */
export function optionForKey(item, key) {
  const index = HOTKEYS.indexOf(key);
  if (index < 0) return null;
  return (item?.options ?? [])[index] ?? null;
}

/** The request body for a decision, so the page never assembles one inline. */
export function decisionBody(optionKey, note, decisionId) {
  const trimmed = (note ?? "").trim();
  const body = trimmed
    ? { option_key: optionKey, note: trimmed }
    : { option_key: optionKey };
  return decisionId ? { ...body, decision_id: decisionId } : body;
}

export function chatBody(note, decisionId) {
  const body = { action: "chat", note: (note ?? "").trim() };
  return decisionId ? { ...body, decision_id: decisionId } : body;
}

/** Stable identity for one exact browser intent, including its bounded note. */
export async function decisionRequestKey(receiptId, body) {
  if (!body?.decision_id)
    throw new Error("an exact decision identity is required");
  const encoded = new TextEncoder().encode(
    JSON.stringify({ receipt_id: receiptId, ...body }),
  );
  const digest = await globalThis.crypto.subtle.digest("SHA-256", encoded);
  const hex = [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  return `browser-v1:${hex}`;
}
