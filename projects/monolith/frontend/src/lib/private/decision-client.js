// Answer one factory decision from the dashboard, with the escalations
// page's exact request protocol (factory/escalations/+page.svelte `send`):
// one immutable request per decision kept in sessionStorage under the same
// key, so a retry, a lost response or a switch between the two pages can
// never send a second, different effect while the first outcome is unknown.
// Only a definitive refusal (4xx) permits a new attempt key.
import {
  contextKey,
  decisionRequestKey,
} from "../../routes/private/factory/escalations/escalations-view.js";

export const storageKey = (item) => `factory-decision:${contextKey(item)}`;

function load(storage, key) {
  try {
    return JSON.parse(storage.getItem(key) ?? "null");
  } catch {
    return null;
  }
}

/**
 * Send `body` (from decisionBody or chatBody) for `item`. Resolves to
 * `{ status, message }` where status is one of:
 * - "accepted": the backend recorded it.
 * - "refused": a definitive refusal; a changed answer may be sent next.
 * - "uncertain": the outcome is unknown; only the same answer may be resent.
 */
export async function sendDecision(item, body, { fetchImpl, storage }) {
  if (!body?.decision_id)
    return {
      status: "refused",
      message: "This card has no exact decision identity. Refresh first.",
    };
  const key = storageKey(item);
  const previous = load(storage, key);
  if (
    previous?.status === "accepted" &&
    (previous.body.action !== "chat" ||
      (body.action === "chat" && body.note === previous.body.note))
  )
    return {
      status: "accepted",
      message: `Already recorded for #${item.issue_number}.`,
    };
  const attempt =
    previous?.status === "refused" || previous?.status === "accepted"
      ? previous.attempt + 1
      : (previous?.attempt ?? 0);
  const requestBody = {
    ...body,
    request_key: await decisionRequestKey(item.receipt_id, body, attempt),
  };
  if (
    previous?.status === "pending" &&
    JSON.stringify(previous.body) !== JSON.stringify(requestBody)
  )
    return {
      status: "uncertain",
      message:
        "An earlier answer's outcome is unknown. Send that same answer again to check it.",
    };
  const record = (status) =>
    storage.setItem(
      key,
      JSON.stringify({ body: requestBody, attempt, status }),
    );
  record("pending");
  let response;
  let result = {};
  try {
    response = await fetchImpl(
      `/factory/escalations/decisions/${item.receipt_id}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(requestBody),
      },
    );
    result = await response.json().catch(() => ({}));
  } catch {
    return {
      status: "uncertain",
      message:
        "Could not confirm the answer. Its outcome is unknown; send the same one again to check.",
    };
  }
  if (!response.ok) {
    const message =
      result.detail ?? result.reason ?? `Refused (${response.status}).`;
    if (response.status >= 400 && response.status < 500) {
      record("refused");
      return { status: "refused", message };
    }
    return { status: "uncertain", message };
  }
  if (result.ok !== true)
    return {
      status: "uncertain",
      message: result.reason ?? "The outcome is unknown.",
    };
  record("accepted");
  if (result.requeued === false)
    return {
      status: "accepted",
      message: `Asked on #${item.issue_number}, but no brief was queued: ${
        result.blocked_by ?? "the lane would not admit it"
      }.`,
    };
  return { status: "accepted", message: `Recorded for #${item.issue_number}.` };
}
