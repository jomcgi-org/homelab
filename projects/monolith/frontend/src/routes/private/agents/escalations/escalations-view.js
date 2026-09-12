// Pure helpers for the escalations page. Everything the page derives from the
// board's escalation list lives here so it is testable without mounting
// Svelte, the same split factory-view.js gives the factory board.

export const EFFECT_WORD = {
  "agent-ready": "deliver",
  close: "close",
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
 * The effect of one option said in a few words, for the line under a button.
 * A split says how many issues it opens, because that is the part a person
 * cannot see from the label and would otherwise have to guess.
 */
export function effectLine(option) {
  if (!option) return "";
  if (option.effect === "split") {
    const n = option.children ?? 0;
    return n === 1
      ? "opens 1 issue, closes this one"
      : `opens ${n} issues, closes this one`;
  }
  return (
    {
      "agent-ready": "labels it agent-ready for the delivery lane",
      close: "closes the issue with a reason",
      defer: "moves it to needs-thought with a wait condition",
      hold: "leaves the issue exactly as it is",
      "escape-close": "closes it as not planned and drops needs-human",
      "escape-defer": "swaps needs-human for needs-thought",
      "escape-dismiss": "clears the card, the issue keeps needs-human",
    }[option.effect] ?? option.effect
  );
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
export function decisionBody(optionKey, note) {
  const trimmed = (note ?? "").trim();
  return trimmed
    ? { option_key: optionKey, note: trimmed }
    : { option_key: optionKey };
}

export function chatBody(note) {
  return { action: "chat", note: (note ?? "").trim() };
}
