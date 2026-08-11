// A run state is terminal when the engine is claiming no further work will
// happen. The complement of the live set means a later state defaults to
// terminal, so disagreement is treated as unconfirmed.
export const LIVE_STATES = new Set([
  "queued",
  "running",
  "reviewing",
  "escalated",
  "blocked",
]);

export function isTerminalState(state) {
  return !LIVE_STATES.has(state);
}

// Five minutes is longer than a single step's reporting gap and shorter than
// the idle time of a genuinely stranded run.
export const TERMINAL_QUIET_SECONDS = 300;

function parsedTime(value) {
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : null;
}

export function freshestActivity(run) {
  let freshest = null;
  for (const node of run?.nodes ?? []) {
    for (const attempt of node.attempts ?? []) {
      const live = attempt.live;
      const observedAt = parsedTime(live?.observed_at);
      if (observedAt == null) continue;
      if (freshest == null || observedAt > freshest.time) {
        freshest = {
          time: observedAt,
          observedAt: live.observed_at,
          nodeLabel: node.label,
          activity: live.activity,
        };
      }
    }
  }
  if (freshest == null) return null;
  const { time, ...observation } = freshest;
  return observation;
}

export function claimStatus(run, now) {
  const terminal = isTerminalState(run?.state) || run?.stranded === true;
  const observation = freshestActivity(run);
  let unconfirmed = false;
  if (terminal && observation) {
    const observedAt = parsedTime(observation.observedAt);
    const completedAt = parsedTime(run?.completed_at);
    const nowTime = parsedTime(now);
    if (completedAt != null) {
      unconfirmed = observedAt > completedAt;
    } else if (nowTime != null) {
      const ageSeconds = (nowTime - observedAt) / 1000;
      unconfirmed = ageSeconds >= 0 && ageSeconds <= TERMINAL_QUIET_SECONDS;
    }
  }
  return { terminal, observation, unconfirmed };
}
