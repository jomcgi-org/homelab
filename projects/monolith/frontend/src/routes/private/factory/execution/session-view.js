export const SESSION_VIEW_CONVERSATION = "conversation";
export const SESSION_VIEW_WALKTHROUGH = "walkthrough";

export function incrementalAfterSeq(turns) {
  const knownTurns = Array.isArray(turns) ? turns : [];
  const maxSeq = Math.max(0, ...knownTurns.map((turn) => turn.seq));
  const newest = knownTurns.find((turn) => turn.seq === maxSeq);
  return newest?.terminal_reason === "interrupted"
    ? Math.max(0, maxSeq - 1)
    : maxSeq;
}

export function mergeTurns(existingTurns, incomingTurns) {
  const incomingBySeq = new Map(
    (Array.isArray(incomingTurns) ? incomingTurns : []).map((turn) => [
      turn.seq,
      turn,
    ]),
  );
  const merged = (Array.isArray(existingTurns) ? existingTurns : []).map(
    (turn) => incomingBySeq.get(turn.seq) ?? turn,
  );
  const existingSeqs = new Set(merged.map((turn) => turn.seq));
  for (const turn of incomingBySeq.values()) {
    if (!existingSeqs.has(turn.seq)) merged.push(turn);
  }
  return merged.sort((left, right) => left.seq - right.seq);
}

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
