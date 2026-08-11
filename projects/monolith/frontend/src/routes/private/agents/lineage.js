import { joinMeta } from "./run-format.js";
import { RUN_LEXICON } from "./run-lexicon.js";

export function sessionLineage(run, sessionId) {
  // Both guards exist because the comparison is stringified: the URL carries
  // ?session= as a string while the payload carries a number, so a strict ===
  // would never match. Without them String(null) === String(null) holds, and
  // ?run= with no session would claim the lineage of the first attempt that
  // never got a session: a fabricated answer, not an absent one.
  if (sessionId == null) return null;
  for (const node of run?.nodes ?? []) {
    for (const attempt of node.attempts ?? []) {
      if (attempt.session_id == null) continue;
      if (String(attempt.session_id) === String(sessionId)) {
        return {
          nodeKey: node.key,
          nodeLabel: node.label,
          attemptN: attempt.n,
        };
      }
    }
  }
  return null;
}

export function crumbTrail({
  kind,
  runTitle,
  nodeLabel,
  attemptN,
  sessionTitle,
}) {
  runTitle = String(runTitle ?? "").trim();
  nodeLabel = String(nodeLabel ?? "").trim();
  sessionTitle = String(sessionTitle ?? "").trim();
  if (kind === "run") {
    return runTitle
      ? [
          { label: RUN_LEXICON.labels.runsWord, to: "home" },
          { label: runTitle, to: null },
        ]
      : [{ label: RUN_LEXICON.labels.runsWord, to: "home" }];
  }
  if (kind !== "session" || !runTitle) return [];

  const leaf =
    joinMeta(
      nodeLabel,
      attemptN != null ? `${RUN_LEXICON.labels.attempt} ${attemptN}` : null,
    ) || sessionTitle;
  return [
    { label: RUN_LEXICON.labels.runsWord, to: "home" },
    { label: runTitle, to: "run" },
    ...(leaf ? [{ label: leaf, to: null }] : []),
  ];
}
