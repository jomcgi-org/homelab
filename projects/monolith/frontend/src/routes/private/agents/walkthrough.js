// Client-side composition for the session-tier walkthrough (ADR 056).
//
// Inputs are the merged payload of GET /api/swarm/compare/{session}/{turn}
// (swarm/compare_router.py, real since PR #4805) plus, when available, the
// parsed rationale trailer in the swarm/rationale.py shape ({parse_status,
// paths: [{path, why}], deviations, raw}). ADR 056 decision 5 puts step
// composition server-side; the server composer had not landed when this
// module was written, so the composition lives here as a pure function with
// the same three inputs the ADR names (compare stats, trailer, classifier)
// and moves behind the composer's output unchanged when it ships. Everything
// here is a view-model transform: no prose is invented, no register is
// recomputed, and the agent's words pass through verbatim.
//
// Output shape (the step shape this surface renders):
//   {
//     rung: 1..5,            // ADR 056 decision 6 ladder
//     trailer: boolean,      // a rationale trailer parsed for this turn
//     ephemeral: boolean,    // rung 2: compare by branch name, has a shelf life
//     declined: boolean,     // rung 4: no walk is offered, stats only
//     steps: [{ path, status, additions, deletions, why, unexplained,
//               patchUrl }],
//     mechanical: { count, files } | null,   // ONE collapsed step, never per file
//     contradicted: [{ path, why }],         // claim without a matching diff file
//     deviations: [string],                  // trailer deviations, testimony
//     touched: [string],                     // rungs 3/4: files touched by tools
//     truncation: [string],                  // server-stated truncation facts
//     stats: { totalFiles, additions, deletions },
//   }

// ADR 056 rung 4: "no compare, no trailer, above roughly a dozen authored
// files" declines to walk. Below the threshold there is still nothing to
// compose a walk from (no diff, no testimony), so the same stats rendering
// applies; the threshold only marks where declining is the deliberate answer
// rather than the vacuous one.
export const DECLINE_FILE_THRESHOLD = 12;

function trailerParsed(rationale) {
  return rationale?.parse_status === "parsed";
}

function truncationFacts(compare) {
  // Truncation renders as a labelled fact, never silent under-reporting.
  // The reasons are server-composed sentences: pass them through as data.
  const facts = [];
  if (compare?.stats?.truncated_reason)
    facts.push(compare.stats.truncated_reason);
  if (compare?.activities_truncated_reason)
    facts.push(compare.activities_truncated_reason);
  return facts;
}

function sumStats(files) {
  let additions = 0;
  let deletions = 0;
  for (const file of files) {
    additions += Number(file.additions || 0);
    deletions += Number(file.deletions || 0);
  }
  return { totalFiles: files.length, additions, deletions };
}

export function composeWalkthrough(compare, rationale = null) {
  // Two distinct facts: the server parsed a trailer for this turn
  // (compare.trailer_parsed), and this view holds its content (rationale).
  // The compare endpoint serves the first without the second; the composer
  // serves both. Cross-check sets key off the first, quoting needs the second.
  const parsed = trailerParsed(rationale);
  const trailerFact = compare?.trailer_parsed === true || parsed;
  const files = Array.isArray(compare?.files) ? compare.files : [];
  const touched = Array.isArray(compare?.authored_file_paths)
    ? compare.authored_file_paths
    : [];
  const truncation = truncationFacts(compare);
  const base = {
    trailer: trailerFact,
    ephemeral: false,
    declined: false,
    steps: [],
    mechanical: null,
    contradicted: [],
    deviations: parsed ? rationale.deviations.filter(Boolean) : [],
    touched,
    truncation,
    stats: sumStats(files),
  };

  if (compare?.resolution_rung !== 1 && compare?.resolution_rung !== 2) {
    // No compare. The server's cross-check sets are differences against an
    // empty diff here (contradicted_paths would be every trailer path), so
    // no cross-check flag is meaningful and none is shown.
    if (parsed) {
      // Rung 3: quoted testimony plus the files tools touched, no diff panes.
      return {
        ...base,
        rung: 3,
        steps: rationale.paths.map((point) => ({
          path: point.path,
          status: null,
          additions: null,
          deletions: null,
          why: point.why || "",
          unexplained: false,
          patchUrl: null,
        })),
      };
    }
    if (touched.length > 0) {
      // Rung 4: stats only, and no walk is offered. That is the decision,
      // not a shortfall (#4614: "declining to offer one is the answer").
      return { ...base, rung: 4, declined: true };
    }
    // Rung 5: nothing at all. The section says so and stops.
    return { ...base, rung: 5 };
  }

  const authored = files.filter((file) => file.classification === "authored");
  const mechanicalFiles = files.filter(
    (file) => file.classification === "mechanical",
  );
  // Per-file "unexplained" flags only mean something when a trailer parsed:
  // with no trailer every file is trivially unmentioned, and that absence is
  // one labelled fact about the turn, not a flag on each of 40 rows.
  const unexplainedSet = trailerFact
    ? new Set(compare.unexplained_files ?? [])
    : new Set();
  const byPath = new Map(authored.map((file) => [file.path, file]));

  // Steps follow the agent's own order first (testimony order is part of the
  // testimony), then remaining authored files in compare order. A mechanical
  // file is never flagged unexplained: the classifier correctly expects no
  // point about it (ADR 056 decision 4).
  const steps = [];
  const seen = new Set();
  const contradicted = [];
  for (const point of parsed ? rationale.paths : []) {
    const file = byPath.get(point.path);
    if (!file) {
      if ((compare.contradicted_paths ?? []).includes(point.path)) {
        contradicted.push({ path: point.path, why: point.why || "" });
      }
      continue;
    }
    if (seen.has(point.path)) continue;
    seen.add(point.path);
    steps.push(stepOf(file, point.why || "", unexplainedSet));
  }
  for (const file of authored) {
    if (seen.has(file.path)) continue;
    seen.add(file.path);
    steps.push(stepOf(file, "", unexplainedSet));
  }
  // A contradiction the trailer names but the parse did not carry a why for
  // (or the trailer is server-parsed only) still renders: juxtaposed, never
  // silently dropped in either direction.
  for (const path of compare.contradicted_paths ?? []) {
    if (!contradicted.some((claim) => claim.path === path)) {
      contradicted.push({ path, why: "" });
    }
  }

  return {
    ...base,
    rung: compare.resolution_rung,
    ephemeral: compare.resolution_rung === 2,
    steps,
    mechanical:
      mechanicalFiles.length > 0
        ? {
            count: mechanicalFiles.length,
            files: mechanicalFiles.map((file) => file.path),
          }
        : null,
    contradicted,
  };
}

function stepOf(file, why, unexplainedSet) {
  return {
    path: file.path,
    status: file.status ?? null,
    additions: Number(file.additions || 0),
    deletions: Number(file.deletions || 0),
    why,
    unexplained: !why && unexplainedSet.has(file.path),
    patchUrl: file.patch_url ?? null,
  };
}

// Splits a GitHub compare `patch` field (unified diff hunks, no file header)
// into typed lines so the component styles without parsing.
export function parsePatch(patch) {
  if (typeof patch !== "string" || patch === "") return [];
  return patch.split("\n").map((text) => ({
    kind: text.startsWith("@@")
      ? "hunk"
      : text.startsWith("+")
        ? "add"
        : text.startsWith("-")
          ? "del"
          : "ctx",
    text,
  }));
}
