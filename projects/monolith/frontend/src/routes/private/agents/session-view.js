export const SESSION_VIEW_CONVERSATION = "conversation";
export const SESSION_VIEW_WALKTHROUGH = "walkthrough";

// The detail response does not carry a synthetic "has walkthrough" flag.
// These are the same durable inputs the composer can turn into something
// useful: agent rationale, a recorded compare range, or tool activity.
export function turnHasWalkthrough(turn) {
  return Boolean(
    turn &&
    (turn.rationale?.parse_status === "parsed" ||
      (turn.base_sha && turn.commit_sha) ||
      (Array.isArray(turn.usage?.activities) &&
        turn.usage.activities.length > 0)),
  );
}

export function walkthroughTurns(turns) {
  return Array.isArray(turns) ? turns.filter(turnHasWalkthrough) : [];
}

export function defaultSessionView(currentVmState, turns) {
  return currentVmState !== "awake" && walkthroughTurns(turns).length > 0
    ? SESSION_VIEW_WALKTHROUGH
    : SESSION_VIEW_CONVERSATION;
}
