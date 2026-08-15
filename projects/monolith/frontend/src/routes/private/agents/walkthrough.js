// View-model for the session-tier walkthrough (ADR 056).
//
// Input is the composed payload of GET /api/swarm/walkthrough/{session}/{turn}
// (swarm/walkthrough_composer.py, PR #4815): the server owns composition and
// epistemics, this module only maps steps to renderable rows. It invents no
// prose and recomputes no register; every string it surfaces is either
// payload data or a lexicon key the component resolves.
//
// Composer step shapes handled:
//   authored      {type, register, file_path, file_change?, testimony?, area?}
//   mechanical    {type, register:"fact", count, generator_activity}
//   unexplained   {type, register:"fact", file_path, file_change, label}
//   contradiction {type, register:"testimony", label, testimony}
//   truncation    {type, register:"fact", label}
// plus payload-level {rung, ephemeral, summary, stats, message?}.

function testimonyPoints(step) {
  const points = step?.testimony?.points;
  return Array.isArray(points) ? points : [];
}

function whyFor(step) {
  const match = testimonyPoints(step).find(
    (point) => point?.path === step.file_path && typeof point.why === "string",
  );
  if (match) return match.why;
  const first = testimonyPoints(step).find(
    (point) => typeof point?.why === "string",
  );
  return first ? first.why : "";
}

function deviationsFor(step) {
  return testimonyPoints(step)
    .map((point) => point?.deviation)
    .filter((value) => typeof value === "string" && value !== "");
}

function attributionFor(step) {
  const testimony = step?.testimony;
  if (!testimony) return null;
  return { turn: testimony.turn ?? null, attempt: testimony.attempt ?? null };
}

function fileChange(step) {
  const change = step?.file_change ?? {};
  return {
    additions: Number(change.additions || 0),
    deletions: Number(change.deletions || 0),
    status: change.status ?? null,
  };
}

// A generator run activity, compacted for a one-line label. Mirrors the
// transcript's activity rendering: command first, then input, then type.
export function generatorLabel(activity) {
  if (activity == null || typeof activity !== "object") return "";
  for (const value of ["command", "input", "detail"]
    .map((name) => activity[name])
    .filter(Boolean)) {
    const text = typeof value === "string" ? value : JSON.stringify(value);
    return text.length > 80 ? `${text.slice(0, 80)}…` : text;
  }
  return typeof activity.type === "string" ? activity.type : "";
}

export function walkthroughView(payload, context = {}) {
  const { sessionId = null, turnSeq = null } = context;
  const rung = payload?.rung ?? 5;
  const steps = Array.isArray(payload?.steps) ? payload.steps : [];
  const stats = payload?.stats ?? {};
  const message = typeof payload?.message === "string" ? payload.message : "";
  const summary =
    payload?.summary != null && typeof payload.summary === "object"
      ? {
          status: payload.summary.status ?? null,
          files: Number(payload.summary.files_changed || 0),
          insertions: Number(payload.summary.insertions || 0),
          deletions: Number(payload.summary.deletions || 0),
          accounted: Number(payload.summary.accounted_files || 0),
          unexplained: Number(payload.summary.unexplained_files || 0),
        }
      : null;

  const authored = steps.filter((step) => step.type === "authored");
  const unexplained = steps.filter((step) => step.type === "unexplained");
  const unexplainedPaths = new Set(unexplained.map((step) => step.file_path));

  // Rung 1/2 patches stay lazy (ADR 056 decision 8): the composer emits no
  // patch URL, so it is derived from the compare patch route (PR #4805),
  // which exists for authored files only. Mechanical and touched-only rows
  // never link a patch.
  const patchPath = (path) =>
    rung <= 2 && sessionId != null && turnSeq != null
      ? `/api/swarm/compare/${encodeURIComponent(sessionId)}/${encodeURIComponent(turnSeq)}/patch?path=${encodeURIComponent(path)}`
      : null;

  const points = [];
  for (const step of authored) {
    const why = whyFor(step);
    // The composer emits an authored row AND an unexplained row for an
    // authored file the trailer skipped. One file, one point: the
    // unexplained row carries the treatment, so the bare twin is dropped.
    // This is presentation dedupe, not a recomputed cross-check.
    if (!why && step.file_path && unexplainedPaths.has(step.file_path)) {
      continue;
    }
    const change = fileChange(step);
    points.push({
      kind: "authored",
      path: step.file_path ?? "",
      area: typeof step.area === "string" ? step.area : null,
      why,
      deviations: deviationsFor(step),
      attribution: attributionFor(step),
      ...change,
      touched: change.status === "touched",
      patchUrl: step.file_path ? patchPath(step.file_path) : null,
    });
  }
  for (const step of unexplained) {
    points.push({
      kind: "unexplained",
      path: step.file_path ?? "",
      area: null,
      why: "",
      deviations: [],
      attribution: null,
      ...fileChange(step),
      touched: false,
      patchUrl: step.file_path ? patchPath(step.file_path) : null,
    });
  }

  const explained = points.filter(
    (point) => point.kind === "authored" && point.why !== "",
  ).length;
  const unexplainedCount = points.filter(
    (point) => point.kind === "unexplained",
  ).length;
  const touchedCount = points.filter(
    (point) => point.kind === "authored" && point.touched && point.why === "",
  ).length;
  let additions = 0;
  let deletions = 0;
  for (const point of points) {
    additions += point.additions;
    deletions += point.deletions;
  }

  return {
    rung,
    ephemeral: payload?.ephemeral === true,
    message,
    summary,
    points,
    mechanical: steps
      .filter((step) => step.type === "mechanical")
      .map((step) => ({
        count: Number(step.count || 0),
        generator: generatorLabel(step.generator_activity),
      })),
    contradictions: steps
      .filter((step) => step.type === "contradiction")
      .map((step) => {
        const point = testimonyPoints(step)[0] ?? {};
        return {
          path: typeof point.path === "string" ? point.path : "",
          why: typeof point.why === "string" ? point.why : "",
          attribution: attributionFor(step),
        };
      }),
    truncations: steps
      .filter((step) => step.type === "truncation")
      .map((step) => (typeof step.label === "string" ? step.label : ""))
      .filter(Boolean),
    // Rung 4's touched list arrives inside stats, labelled server-side.
    touched: Array.isArray(stats.activities) ? stats.activities : [],
    counts: {
      points: explained,
      accounted: explained,
      unexplained: unexplainedCount,
      contradicted: steps.filter((step) => step.type === "contradiction")
        .length,
      touched: touchedCount,
      files: Number(stats.total_files ?? stats.authored_files ?? points.length),
      additions,
      deletions,
    },
    hasTestimony:
      explained > 0 || steps.some((step) => step.type === "contradiction"),
  };
}

// Splits a GitHub compare `patch` field (unified diff, no file header) into
// hunks for the filebar-and-hunks rendering: each hunk keeps its @@ header
// and typed lines with a one-character gutter.
export function parsePatchHunks(patch) {
  if (typeof patch !== "string" || patch === "") return [];
  const hunks = [];
  for (const raw of patch.split("\n")) {
    if (raw.startsWith("@@") || hunks.length === 0) {
      hunks.push({ header: raw.startsWith("@@") ? raw : "", lines: [] });
      if (raw.startsWith("@@")) continue;
    }
    const kind = raw.startsWith("+")
      ? "add"
      : raw.startsWith("-")
        ? "del"
        : "ctx";
    hunks[hunks.length - 1].lines.push({
      kind,
      gutter: kind === "add" ? "+" : kind === "del" ? "-" : " ",
      text: raw.slice(kind === "ctx" && !raw.startsWith(" ") ? 0 : 1),
    });
  }
  return hunks;
}
