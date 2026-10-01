/**
 * Pure derivations for /slop/factory/activity, its task pages, and its session
 * records. Everything the three views need that is not markup lives here:
 * formatting against an explicit `now`, the state vocabulary, the plan strip,
 * and the activity rows. Diffs parse in diff.js and markdown in markdown.js.
 * The Svelte components stay dumb, so every rule below is testable without a
 * DOM and without a clock.
 */

// The factory records commits by SHA only, so the record has to name the repo
// itself to turn one into a link.
const REPO = "https://github.com/jomcgi-org/homelab";

const MS_PER_SECOND = 1000;
const SECONDS_PER_HOUR = 3600;
const SECONDS_PER_MINUTE = 60;
const MS_PER_DAY = 86_400_000;
const RECENT_WINDOW_DAYS = 7;
// A prompt or a reply past this many characters is collapsed by default.
const TEXT_CLIP = 600;

// One square, one meaning. A task state and a node state are different
// vocabularies over the same six marks, so each gets its own map rather than a
// shared one that would quietly accept the wrong word.
const TASK_MARK = {
  "in flight": "running live",
  // A landing task is still live: its PR is enqueued and the lane holds the
  // slot until the merge settles, so it breathes like a running one.
  landing: "running live",
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

/** A task is live while the lane still holds its slot. */
export const LIVE_STATES = new Set(["in flight", "landing", "uncertain"]);

export function commitUrl(sha) {
  return sha ? `${REPO}/commit/${sha}` : null;
}

export function money(value) {
  return `$${(Number(value) || 0).toFixed(2)}`;
}

/** Token counts are read at a glance, so thousands and millions collapse to
 * one decimal. */
export function tokens(value) {
  const count = Number(value) || 0;
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
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
 * Completed tasks sort newest first. The seven-day strip comes from the
 * snapshot's totals_7d, counted over every receipt and every factory turn at
 * list price; `recent` is capped at a dozen tasks, so it is only the fallback
 * for a snapshot written before totals_7d existed.
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
  const totals = activity?.totals_7d;
  if (totals) {
    return {
      live,
      queued,
      done,
      landed: Number(totals.landed) || 0,
      escalated: Number(totals.escalated) || 0,
      spend: Number(totals.spend_usd) || 0,
    };
  }
  const spend = [...week, ...live].reduce(
    (total, task) => total + (Number(task.cost_usd) || 0),
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
  const pr = task.pr ? `PR #${task.pr.number}` : null;
  if (task.state === "queued") {
    return `queued · ${plural(policy?.max_tasks ?? 0, "slot")}`;
  }
  if (task.state === "in flight" || task.state === "uncertain") {
    return `${task.state} · ${task.phase} · ${relative(task.deadline_at, now)}`;
  }
  if (task.state === "landing") {
    return [`landing`, pr && `${pr} ${task.pr.state}`, rounds]
      .filter(Boolean)
      .join(" · ");
  }
  if (task.state === "landed") {
    return [`landed`, pr && `${pr} merged`, rounds].filter(Boolean).join(" · ");
  }
  if (task.state === "failed") {
    return `escalated · ${rounds}`;
  }
  return `cancelled · ${rounds}`;
}

/**
 * The task's verdict in pieces the view can render: plain text, a link, an
 * inline code span, or a link to one of the steps below it. The words are the
 * record's own (an evidence reason, a stop reason) wherever it has them.
 */
export function outcome(task, policy, now, runningStep = -1) {
  const rounds = plural(reviewRounds(task), "review round");
  const mark = taskMark(task.state);
  const prLink = task.pr
    ? [{ text: "PR " }, { text: `#${task.pr.number}`, href: task.pr.url }]
    : [];
  if (task.state === "landed") {
    return {
      tone: "landed",
      headline: "landed",
      mark,
      parts: [
        ...prLink,
        {
          text: `${task.pr ? " merged · " : ""}${rounds} · ${duration(task.admitted_at, task.finished_at)}`,
        },
      ],
    };
  }
  if (task.state === "failed") {
    return {
      tone: "failed",
      headline: "escalated",
      mark,
      parts: [{ text: task.evidence_reason || "Waits for a person." }],
    };
  }
  if (task.state === "cancelled") {
    return {
      tone: "cancelled",
      headline: "cancelled",
      mark,
      parts: [
        {
          text:
            task.evidence_reason ||
            task.stop_events?.[0]?.reason ||
            "Cancelled by an operator.",
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
          text: `Waits for a slot · ${plural(policy?.max_tasks ?? 0, "slot")}`,
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
        { text: "Outcome unknown at " },
        { text: task.phase, code: true },
        { text: " · holds its slot until reconciled" },
      ],
    };
  }
  if (task.state === "landing") {
    return {
      tone: "live",
      headline: "landing",
      mark,
      parts: [
        ...prLink,
        {
          text: `${task.pr ? ` ${task.pr.state} · ` : ""}${rounds} · ${relative(task.deadline_at, now)}`,
        },
      ],
    };
  }
  const parts = [{ text: "At " }, { text: task.phase, code: true }];
  if (runningStep >= 0) {
    parts.push(
      { text: " (" },
      { text: `step ${runningStep + 1}`, step: runningStep + 1 },
      { text: ")" },
    );
  }
  if (task.pr) {
    parts.push({ text: " · " }, ...prLink, { text: ` ${task.pr.state}` });
  }
  parts.push({
    text: ` · ${task.turns_used} of ${task.allowance_turns} starts · ${relative(task.deadline_at, now)}`,
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
      value: `${money(task.cost_usd)} of ${money(policy?.task_budget_usd)}`,
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
 * Long text, cut for a first read. A conductor prompt runs to thousands of
 * characters and buries the reply under it, so the view shows a head and offers
 * the rest. The cut lands on the last whitespace before the limit so it ends on
 * a word, and `head + rest` is always the original text.
 */
export function clip(text, limit = TEXT_CLIP) {
  const full = text ?? "";
  if (full.length <= limit) return { head: full, rest: "", clipped: false };
  const window = full.slice(0, limit);
  // The last run of whitespace in the window, or none at all in one long token.
  const at = window.search(/\s+(?=\S*$)/);
  const cut = at > 0 ? at : limit;
  return { head: full.slice(0, cut), rest: full.slice(cut), clipped: true };
}

/**
 * The plan as a strip of nodes in the order the engine applied them, one
 * entry per node with its attempts folded in. A DAG figure was tried here and
 * dropped: real plans are chains of conductor turns with no declared
 * dependencies, which laid out as one tall column of boxes. The dependencies
 * that do exist are named on the entry instead.
 */
export function planStrip(task, steps = []) {
  return (task?.nodes ?? []).map((node, index) => {
    const attempts = node.attempts ?? [];
    const first = steps.findIndex((step) => step.node === node);
    return {
      number: index + 1,
      node,
      step: first >= 0 ? first + 1 : null,
      attempts: attempts.length,
      cost: attempts.reduce(
        (sum, attempt) => sum + (Number(attempt.cost_usd) || 0),
        0,
      ),
      deps: node.deps ?? [],
    };
  });
}

/** Stop events newest first, with their stamps split for a table. */
export function stopRows(task) {
  return [...(task?.stop_events ?? [])]
    .sort(
      (a, b) => new Date(b.at ?? 0).getTime() - new Date(a.at ?? 0).getTime(),
    )
    .map((event) => ({
      day: isoDay(event.at),
      clock: isoClock(event.at),
      action: event.action ?? "",
      reason: event.reason ?? "",
      person: Boolean(event.intervention_required),
    }));
}

/**
 * A reply without its rationale trailer, when the record parsed one out: the
 * trailer is shown as its own block, so the reply must not carry it twice.
 */
export function stripRationale(text, rationale) {
  const body = text ?? "";
  const raw = rationale?.raw;
  if (!raw || !body.endsWith(raw)) return body;
  return body.slice(0, body.length - raw.length).replace(/\s+$/, "");
}

// The shim names MCP tools server__tool; the reader wants the tool and, in
// second place, where it lives.
const MCP_TOOL = /^mcp__([^_]+(?:_[^_]+)*)__(.+)$/;

/** A tool name as a reader says it. */
export function toolLabel(name) {
  const text = name ?? "tool";
  const mcp = MCP_TOOL.exec(text);
  return mcp ? `${mcp[2]} · ${mcp[1]}` : text;
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

// The Codex runtime does not record the command a worker asked for. It records
// its own shell wrapper around a quoted argv: `/bin/sh -lc '"git" "remote"
// "-v"'`, and inside a double-quoted wrapper each argv quote arrives escaped.
// The wrapper is the runtime's business, not the reader's.
const SHELL_WRAPPER = /^\/bin\/(?:ba)?sh -lc (['"])([\s\S]*)\1$/;
// An inner string that is nothing but double-quoted tokens, and the tokens.
const ARGV_LINE = /^"(?:[^"\\]|\\.)*"(?: +"(?:[^"\\]|\\.)*")*$/;
const ARGV_TOKEN = /"((?:[^"\\]|\\.)*)"/g;

/**
 * A command as a reader would have typed it. Only the two wrapper shapes above
 * are unwrapped, and only an inner string that is entirely quoted argv words is
 * rejoined: a heredoc or a shell line with its own quoting comes back as it
 * went in, minus the wrapper. Never throws, because the string is worker output
 * and a malformed one still has to render.
 */
export function prettyCommand(command) {
  // Worker output, so the shape is not guaranteed: anything that is not a
  // string is named rather than thrown over.
  if (typeof command !== "string")
    return command == null ? "" : String(command);
  const wrapper = SHELL_WRAPPER.exec(command);
  if (!wrapper) return command;
  const inner = wrapper[2];
  const argv = wrapper[1] === '"' ? inner.replace(/\\"/g, '"') : inner;
  if (!ARGV_LINE.test(argv)) return inner;
  return [...argv.matchAll(ARGV_TOKEN)]
    .map((token) => token[1].replace(/\\(.)/g, "$1"))
    .join(" ");
}

/**
 * One activity row. An edit or a write points at a file, which the view can
 * open out of the turn diff; a command or a tool call has nothing to open and
 * stays a plain row.
 */
export function activityRow(activity) {
  // A tool row is labelled "tool" and names the tool in the value column; the
  // name used to sit in both columns, where a long one overran the label's
  // fixed width and collided with itself. A bash row the shim recorded without
  // its command says so rather than showing nothing.
  const type = activity.type === "tool_use" ? "tool" : (activity.type ?? "");
  const detail =
    activity.command != null
      ? prettyCommand(activity.command)
      : (activity.file_path ?? null);
  const what =
    activity.type === "tool_use"
      ? [toolLabel(activity.name), detail].filter(Boolean).join(" ")
      : (detail ?? "(command not recorded)");
  // The turn digest has one line for every activity, so a path there is its
  // last segment; the full path stays on the row itself and in the title.
  const short = activity.file_path
    ? activity.file_path.split("/").filter(Boolean).pop() || what
    : what;
  const opens = activity.type === "edit" || activity.type === "write";
  return {
    type,
    what,
    short,
    path: opens ? (activity.file_path ?? null) : null,
  };
}

// The digest on the task page names kinds in the vocabulary the rows already
// use, in the order a reader cares about them, and shows only the first few.
// 80 characters is about what the column holds at 0.72rem mono.
const DIGEST_ROWS = 4;
const DIGEST_WIDTH = 80;
const KIND_ORDER = ["edit", "write", "command", "read", "tool call"];
// The one tool call worth naming by what it did rather than by what it is.
const READ_TOOL = /read/i;

function digestKind(activity) {
  if (activity.type === "edit") return "edit";
  if (activity.type === "write") return "write";
  if (activity.type === "bash") return "command";
  if (activity.type === "tool_use") {
    return READ_TOOL.test(activity.name ?? "") ? "read" : "tool call";
  }
  return activity.type || "activity";
}

/**
 * One digest row is one line. A heredoc's newlines would otherwise break the
 * column, and anything past the width ends in a single ellipsis.
 */
function oneLine(text, width = DIGEST_WIDTH) {
  const flat = (text ?? "").replace(/\s+/g, " ").trim();
  return flat.length <= width ? flat : `${flat.slice(0, width - 1)}…`;
}

/**
 * What a turn did, counted, plus its first few rows and how many are left. A
 * Codex-runtime turn records over a hundred activities, and the task page used
 * to print every one of them on one dotted line. The list itself belongs on the
 * session record; the task page gets this.
 */
export function activitySummary(activities, limit = DIGEST_ROWS) {
  const all = activities ?? [];
  const tally = new Map();
  for (const activity of all) {
    const kind = digestKind(activity);
    tally.set(kind, (tally.get(kind) ?? 0) + 1);
  }
  // A kind the vocabulary does not know sorts last rather than disappearing.
  const rank = (kind) => {
    const at = KIND_ORDER.indexOf(kind);
    return at < 0 ? KIND_ORDER.length : at;
  };
  const counts = [...tally.entries()]
    .map(([kind, count]) => ({ kind, count }))
    .sort((a, b) => rank(a.kind) - rank(b.kind));
  const shown = all.slice(0, Math.max(0, limit)).map((activity) => {
    const row = activityRow(activity);
    return { type: row.type, text: oneLine(row.short), title: row.what };
  });
  return { counts, shown, hidden: all.length - shown.length };
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
