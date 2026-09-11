/**
 * Pure derivations for /slop/factory/activity, its task pages, and its session
 * records. Everything the three views need that is not markup lives here:
 * formatting against an explicit `now`, the state vocabulary, the plan layout,
 * and diff parsing. The Svelte components stay dumb, so every rule below is
 * testable without a DOM and without a clock.
 */

// The factory records commits by SHA only, so the record has to name the repo
// itself to turn one into a link.
const REPO = "https://github.com/jomcgi-org/homelab";

const MS_PER_SECOND = 1000;
const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_MINUTE = 60;
const MS_PER_DAY = 86_400_000;
const RECENT_WINDOW_DAYS = 7;

// One square, one meaning. A task state and a node state are different
// vocabularies over the same six marks, so each gets its own map rather than a
// shared one that would quietly accept the wrong word.
const TASK_MARK = {
  "in flight": "running live",
  uncertain: "uncertain",
  landed: "landed",
  failed: "failed",
  cancelled: "cancelled",
  queued: "queued",
};

const NODE_WORD = {
  done: "done",
  running: "running",
  pending: "queued",
  failed: "failed",
  uncertain: "uncertain",
  cancelled: "cancelled",
  retired: "retired",
};

// Plan figure geometry, in the SVG's own user units. The viewBox scales it, so
// these are ratios rather than pixels: a box wide enough for a node key at
// 11px mono, a column gap wide enough for an elbow and its arrowhead.
const NODE_WIDTH = 150;
const NODE_HEIGHT = 44;
const COLUMN_GAP = 44;
const ROW_GAP = 16;
const FIGURE_PAD = 16;
const ARROW_LENGTH = 6;
const ARROW_HALF_HEIGHT = 4;

export function commitUrl(sha) {
  return sha ? `${REPO}/commit/${sha}` : null;
}

export function money(value) {
  return `$${(Number(value) || 0).toFixed(2)}`;
}

/** Token counts are read at a glance, so thousands collapse to one decimal. */
export function tokens(value) {
  const count = Number(value) || 0;
  return count >= 1000 ? `${(count / 1000).toFixed(1)}k` : String(count);
}

export function plural(count, word, suffix = "s") {
  return `${count} ${word}${count === 1 ? "" : suffix}`;
}

/**
 * Timestamps are ISO 8601 UTC and are shown in UTC, not the reader's zone: the
 * lane's deadlines, the policy windows and the issue timestamps are all UTC,
 * and a record that silently shifts is worse than one that names its zone.
 */
export function isoDay(iso) {
  return iso ? new Date(iso).toISOString().slice(0, 10) : "";
}

export function isoClock(iso) {
  return iso ? new Date(iso).toISOString().slice(11, 16) : "";
}

export function stampUtc(iso) {
  return iso ? `${isoDay(iso)} ${isoClock(iso)} UTC` : "";
}

function splitHoursMinutes(totalSeconds) {
  const hours = Math.floor(totalSeconds / SECONDS_PER_HOUR);
  const minutes = Math.floor(
    (totalSeconds % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE,
  );
  return hours ? `${hours}h ${minutes}m` : `${minutes}m`;
}

/** "3h 12m ago" behind `now`, "5h 42m left" ahead of it. */
export function relative(iso, now) {
  if (!iso) return "";
  const delta =
    (new Date(now).getTime() - new Date(iso).getTime()) / MS_PER_SECOND;
  const width = splitHoursMinutes(Math.abs(delta));
  return delta >= 0 ? `${width} ago` : `${width} left`;
}

export function duration(from, to) {
  if (!from || !to) return "";
  const delta =
    (new Date(to).getTime() - new Date(from).getTime()) / MS_PER_SECOND;
  return splitHoursMinutes(Math.max(0, delta));
}

export function taskMark(state) {
  return TASK_MARK[state] ?? "queued";
}

export function nodeWord(state) {
  return NODE_WORD[state] ?? state ?? "";
}

/** The store admits an attempt before its first turn returns; a reader calls
 * that running. Every other status is already the word for it. */
export function attemptWord(status) {
  return status === "admitted" ? "running" : (status ?? "");
}

export function attemptMark(status) {
  if (attemptWord(status) === "running") return "running";
  return status === "succeeded" ? "done" : (status ?? "queued");
}

/**
 * Review rounds come from the snapshot, which counts the correct_<n> keys the
 * engine inserted. Node kind is not usable here: a correction node has kind
 * "work" like any other work node.
 */
export function reviewRounds(task) {
  return task?.review_rounds ?? 0;
}

/** One step per attempt, in plan order then attempt order. */
export function stepsOf(task) {
  const steps = [];
  for (const node of task?.nodes ?? []) {
    for (const attempt of node.attempts ?? []) steps.push({ node, attempt });
  }
  return steps;
}

/**
 * Split the board payload into the two ledgers and the numbers above them.
 * Completed tasks sort newest first; the seven-day counts are what the strip
 * reports, and spend counts committed cost on live tasks too because that money
 * is already gone.
 */
export function ledger(activity, now) {
  const live = activity?.active ?? [];
  const queued = activity?.queued ?? [];
  const done = [...(activity?.recent ?? [])].sort(
    (a, b) =>
      new Date(b.finished_at ?? 0).getTime() -
      new Date(a.finished_at ?? 0).getTime(),
  );
  const edge = new Date(now).getTime() - RECENT_WINDOW_DAYS * MS_PER_DAY;
  const week = done.filter(
    (task) => task.finished_at && new Date(task.finished_at).getTime() >= edge,
  );
  const spend = [...week, ...live].reduce(
    (total, task) => total + (Number(task.committed_cost_usd) || 0),
    0,
  );
  return {
    live,
    queued,
    done,
    landed: week.filter((task) => task.state === "landed").length,
    escalated: week.filter((task) => task.state === "failed").length,
    spend,
  };
}

/** The completed ledger rules off a day at a time, newest first. */
export function groupByDay(tasks) {
  const days = [];
  for (const task of tasks) {
    const day = isoDay(task.finished_at);
    const current = days[days.length - 1];
    if (current && current.day === day) current.tasks.push(task);
    else days.push({ day, tasks: [task] });
  }
  return days;
}

/** The second line of a ledger row: where the task is, in one clause each. */
export function ledgerMeta(task, policy, now) {
  const rounds = plural(reviewRounds(task), "review round");
  if (task.state === "queued") {
    return `queued · waits for a slot, ${plural(policy?.max_tasks ?? 0, "task")} at a time`;
  }
  if (task.state === "in flight" || task.state === "uncertain") {
    return `${task.state} · at ${task.phase} · deadline ${relative(task.deadline_at, now)}`;
  }
  if (task.state === "landed") {
    return `landed · PR #${task.pr?.number ?? "?"} merged · ${rounds}`;
  }
  if (task.state === "failed") {
    return `escalated · waits for a person · ${rounds}`;
  }
  return `cancelled by an operator · ${rounds}`;
}

/**
 * The task's verdict as a sentence, in pieces the view can render: plain text,
 * a link, an inline code span, or a link to one of the steps below it. Building
 * it here rather than in the template keeps the wording in one place and under
 * test.
 */
export function outcome(task, policy, now, runningStep = -1) {
  const rounds = plural(reviewRounds(task), "review round");
  const mark = taskMark(task.state);
  if (task.state === "landed") {
    return {
      tone: "landed",
      headline: "landed",
      mark,
      parts: [
        { text: "PR " },
        { text: `#${task.pr?.number ?? "?"}`, href: task.pr?.url },
        {
          text: ` merged to main after ${rounds}, ${duration(task.admitted_at, task.finished_at)} from admission.`,
        },
      ],
    };
  }
  if (task.state === "failed") {
    const reason = task.evidence_reason ? `${task.evidence_reason}. ` : "";
    return {
      tone: "failed",
      headline: "escalated",
      mark,
      parts: [
        {
          text: `${reason}The lane did not retry: an escalation asks for a person, so the task waits for one.`,
        },
      ],
    };
  }
  if (task.state === "cancelled") {
    return {
      tone: "cancelled",
      headline: "cancelled",
      mark,
      parts: [
        {
          text: task.stop_events?.[0]?.reason ?? "Cancelled by an operator.",
        },
      ],
    };
  }
  if (task.state === "queued") {
    return {
      tone: "",
      headline: "queued",
      mark,
      parts: [
        {
          text: `Waiting for a slot. The lane runs ${plural(policy?.max_tasks ?? 0, "task")} at a time; the conductor plans once one opens.`,
        },
      ],
    };
  }
  if (task.state === "uncertain") {
    return {
      tone: "uncertain",
      headline: "uncertain",
      mark,
      parts: [
        {
          text: `An attempt at ${task.phase} has an unknown outcome. It keeps its whole reservation until someone reconciles it; nothing else starts on this task meanwhile.`,
        },
      ],
    };
  }
  const parts = [{ text: "Now at " }, { text: task.phase, code: true }];
  if (runningStep >= 0) {
    parts.push(
      { text: " (" },
      { text: `step ${runningStep + 1}`, step: runningStep + 1 },
      { text: ")" },
    );
  }
  if (task.pr) {
    parts.push(
      { text: ", draft PR " },
      { text: `#${task.pr.number}`, href: task.pr.url },
      { text: " open" },
    );
  }
  parts.push({
    text: `. ${task.turns_used} of ${task.allowance_turns} starts used, ${relative(task.deadline_at, now)} on the deadline.`,
  });
  return { tone: "live", headline: "in flight", mark, parts };
}

/** The boxed spec beside the task title: seven rows, label then value. */
export function taskSpec(task, policy, now) {
  const elapsed = task.admitted_at
    ? task.finished_at
      ? duration(task.admitted_at, task.finished_at)
      : `${relative(task.admitted_at, now).replace(" ago", "")} so far`
    : "–";
  return [
    { label: "State", value: task.state, mark: taskMark(task.state) },
    { label: "Phase", value: task.phase },
    {
      label: "Starts",
      value: `${task.turns_used} of ${task.allowance_turns}`,
      note: `(hard ${policy?.max_task_turns_hard ?? "?"})`,
      num: true,
    },
    {
      label: "Spend",
      value: `${money(task.committed_cost_usd)} of ${money(policy?.task_budget_usd)}`,
      num: true,
    },
    { label: "Elapsed", value: elapsed, num: true },
    {
      label: "Rounds",
      value: `${reviewRounds(task)} of ${policy?.max_review_rounds ?? "?"}`,
      num: true,
    },
    {
      label: "Admitted",
      value: task.admitted_at ? stampUtc(task.admitted_at) : "not yet",
      num: true,
    },
  ];
}

/** The boxed spec beside a session title. The attempt count sits in the
 * heading above it, so it is not repeated here. */
export function sessionSpec(session) {
  return [
    { label: "Session", value: session.key },
    { label: "Model", value: session.model },
    {
      label: "Status",
      value: attemptWord(session.status),
      mark: attemptMark(session.status),
    },
    { label: "Turns", value: String(session.turn_count ?? 0), num: true },
    { label: "Cost", value: money(session.cost_usd), num: true },
    { label: "Guest", value: session.guest_bound ? "bound" : "none" },
    {
      label: "Started",
      value: session.created_at ? stampUtc(session.created_at) : "–",
      num: true,
    },
    {
      label: "Last turn",
      value: session.last_turn_at ? stampUtc(session.last_turn_at) : "–",
      num: true,
    },
    {
      label: "Ended",
      value: session.terminal_reason ?? "still running",
    },
  ];
}

/**
 * Rank each node one past its deepest dependency, then lay the ranks out as
 * columns left to right. Returns absolute geometry so the figure is a `each`
 * over boxes and paths rather than a script inside the template.
 */
export function planLayout(nodes = [], steps = []) {
  if (!nodes.length) {
    return { nodes: [], edges: [], width: 0, height: 0, stages: 0 };
  }
  const ranks = new Map();
  const parentsOf = (node) =>
    (node.deps ?? []).flatMap((key) =>
      nodes.filter((candidate) => candidate.node_key === key),
    );
  const rankOf = (node, seen = new Set()) => {
    if (ranks.has(node)) return ranks.get(node);
    // A dependency cycle would otherwise recurse forever. The conductor does
    // not emit one, but the figure must not be the thing that finds out.
    if (seen.has(node)) return 0;
    seen.add(node);
    const parents = parentsOf(node);
    const rank = parents.length
      ? 1 + Math.max(...parents.map((parent) => rankOf(parent, seen)))
      : 0;
    ranks.set(node, rank);
    return rank;
  };
  nodes.forEach((node) => rankOf(node));

  const columns = new Map();
  for (const node of nodes) {
    const rank = ranks.get(node);
    if (!columns.has(rank)) columns.set(rank, []);
    columns.get(rank).push(node);
  }
  const stages = columns.size;
  const tallest = Math.max(...[...columns.values()].map((c) => c.length));
  const width =
    FIGURE_PAD * 2 + stages * NODE_WIDTH + (stages - 1) * COLUMN_GAP;
  const height =
    FIGURE_PAD * 2 + tallest * NODE_HEIGHT + (tallest - 1) * ROW_GAP;

  const placed = new Map();
  const laid = nodes.map((node, index) => {
    const rank = ranks.get(node);
    const row = columns.get(rank).indexOf(node);
    const box = {
      node,
      index,
      number: index + 1,
      x: FIGURE_PAD + rank * (NODE_WIDTH + COLUMN_GAP),
      y: FIGURE_PAD + row * (NODE_HEIGHT + ROW_GAP),
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
      step: steps.findIndex((step) => step.node === node),
    };
    placed.set(node, box);
    return box;
  });

  const edges = [];
  for (const box of laid) {
    for (const parent of parentsOf(box.node)) {
      const from = placed.get(parent);
      const x1 = from.x + NODE_WIDTH;
      const y1 = from.y + NODE_HEIGHT / 2;
      const x2 = box.x;
      const y2 = box.y + NODE_HEIGHT / 2;
      const elbow = x1 + COLUMN_GAP / 2;
      edges.push({
        d: `M${x1} ${y1} H${elbow} V${y2} H${x2 - 5}`,
        arrow: `M${x2 - ARROW_LENGTH} ${y2 - ARROW_HALF_HEIGHT} L${x2} ${y2} L${x2 - ARROW_LENGTH} ${y2 + ARROW_HALF_HEIGHT} Z`,
        dead: parent.state === "failed",
      });
    }
  }
  return { nodes: laid, edges, width, height, stages };
}

/** files, additions and deletions from a unified diff, or null for no diff. */
export function diffStat(diff) {
  if (!diff) return null;
  let files = 0;
  let additions = 0;
  let deletions = 0;
  for (const line of diff.split("\n")) {
    if (line.startsWith("diff --git")) files += 1;
    else if (line.startsWith("+++") || line.startsWith("---")) continue;
    else if (line.startsWith("+")) additions += 1;
    else if (line.startsWith("-")) deletions += 1;
  }
  return { files, additions, deletions };
}

/** The one file's slice of a multi-file diff, matched on its post-image path. */
export function hunkFor(diff, path) {
  if (!diff || !path) return null;
  const parts = diff.split(/(?=^diff --git )/m);
  return parts.find((part) => part.includes(` b/${path}`)) ?? null;
}

/**
 * Classify a diff into rows the view paints. Rows, never HTML: the diff is
 * worker output, so it must never be able to reach the page as markup.
 */
export function diffLines(diff) {
  if (!diff) return [];
  return diff.split("\n").map((text) => {
    if (text.startsWith("diff --git")) return { cls: "fn", text };
    if (
      text.startsWith("+++") ||
      text.startsWith("---") ||
      text.startsWith("@@")
    ) {
      return { cls: "hd", text };
    }
    if (text.startsWith("+")) return { cls: "add", text };
    if (text.startsWith("-")) return { cls: "del", text };
    return { cls: "", text };
  });
}

/**
 * One activity row. An edit or a write points at a file, so it can open that
 * file's hunk out of the turn diff; a command or a tool call has nothing to
 * open and stays a plain row.
 */
export function activityRow(activity, diff) {
  const type =
    activity.type === "tool_use"
      ? (activity.name ?? "tool").toLowerCase()
      : (activity.type ?? "");
  const what = activity.command ?? activity.file_path ?? activity.name ?? "";
  // The turn digest has one line for every activity, so a path there is its
  // last segment; the full path stays on the row itself and in the title.
  const short = activity.file_path
    ? activity.file_path.split("/").filter(Boolean).pop() || what
    : what;
  const opens = activity.type === "edit" || activity.type === "write";
  const hunk = opens ? hunkFor(diff, activity.file_path) : null;
  return { type, what, short, hunk };
}

/** The grey line under a turn: what it cost, what it wrote, how it ended. */
export function turnMeta(turn) {
  const parts = [{ text: money(turn.cost_usd) }];
  const usage = turn.usage;
  if (usage && usage.input_tokens != null) {
    const cached = usage.cache_read_tokens
      ? ` · ${tokens(usage.cache_read_tokens)} cached`
      : "";
    parts.push({
      text: `${tokens(usage.input_tokens)} in · ${tokens(usage.output_tokens)} out${cached}`,
    });
  }
  if (turn.commit_sha) {
    parts.push({
      text: "commit ",
      sha: turn.commit_sha,
      baseSha: turn.base_sha ?? null,
    });
  }
  const stat = diffStat(turn.diff);
  if (stat) parts.push({ text: "", stat });
  // A clipped diff still counts the lines it kept, so say so rather than let
  // the stat read as the whole change.
  if (turn.diff_truncated) parts.push({ text: "diff truncated", bad: true });
  if (turn.stop_reason) parts.push({ text: "stop ", strong: turn.stop_reason });
  if (turn.terminal_reason && turn.terminal_reason !== "completed") {
    parts.push({ text: turn.terminal_reason, bad: true });
  }
  const denials = turn.permission_denials ?? [];
  if (denials.length) {
    parts.push({
      text: plural(denials.length, "permission denial"),
      bad: true,
    });
  }
  return parts;
}

/** The link to a session record, with the node key kept intact through the path. */
export function sessionHref(issue, nodeKey, attempt) {
  return `/slop/factory/activity/${issue}/${encodeURIComponent(nodeKey)}/${attempt}`;
}
