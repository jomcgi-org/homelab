const DAY_MS = 24 * 60 * 60 * 1000;

function dateParts(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value ?? "");
  if (!match) return null;
  return {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
  };
}

function dateStamp(value) {
  const parts = dateParts(value);
  if (!parts) return null;
  return Date.UTC(parts.year, parts.month - 1, parts.day);
}

function todayStamp(now) {
  return Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
}

function formatDate(value, options) {
  const stamp = dateStamp(value);
  if (stamp == null) return "an unknown date";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    ...options,
  }).format(new Date(stamp));
}

export function moveCountdown(spans, now = new Date()) {
  const moves = (spans ?? [])
    .filter((span) => span.kind === "move" && dateStamp(span.starts_on) != null)
    .toSorted((left, right) => left.starts_on.localeCompare(right.starts_on));

  if (moves.length === 0) {
    return {
      headline: "No move date set",
      detail: "Add a move span when the date is known.",
    };
  }

  const move = moves[0];
  const days = Math.round(
    (dateStamp(move.starts_on) - todayStamp(now)) / DAY_MS,
  );
  let headline;
  if (days === 0) headline = "Moving day";
  else if (days === 1) headline = "1 day to go";
  else if (days > 1) headline = `${days} days to go`;
  else if (days === -1) headline = "1 day since move day";
  else headline = `${Math.abs(days)} days since move day`;

  return {
    headline,
    detail: formatDate(move.starts_on, {
      weekday: "long",
      day: "numeric",
      month: "long",
      year: "numeric",
    }),
  };
}

export function progressSummary(progress, tasks) {
  const items = Array.isArray(tasks) ? tasks : [];
  const total = items.length;
  const done = items.filter((task) => task.done_at != null).length;
  const numeric = Number(progress);
  const value = Number.isFinite(numeric)
    ? Math.min(1, Math.max(0, numeric))
    : 0;
  return {
    done,
    total,
    value,
    percent: Math.round(value * 100),
    label: `${done} of ${total} done`,
  };
}

export function sortOpenTasks(tasks) {
  return (tasks ?? [])
    .filter((task) => task.done_at == null)
    .toSorted((left, right) => {
      if (left.due_on == null && right.due_on == null) return 0;
      if (left.due_on == null) return 1;
      if (right.due_on == null) return -1;
      return left.due_on.localeCompare(right.due_on);
    });
}

export function formatTaskDueDate(value) {
  if (!value) return "No due date";
  return formatDate(value, {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function formatCollisionRange(from, to) {
  const start = dateParts(from);
  const end = dateParts(to);
  if (!start || !end) return "dates unknown";
  if (from === to) {
    return formatDate(from, { day: "numeric", month: "long" });
  }
  if (start.year === end.year && start.month === end.month) {
    return `${start.day} to ${end.day} ${formatDate(to, { month: "long" })}`;
  }
  return `${formatDate(from, {
    day: "numeric",
    month: "long",
  })} to ${formatDate(to, { day: "numeric", month: "long" })}`;
}

export function collisionWording(collision, tasks, spans) {
  const tasksById = new Map((tasks ?? []).map((task) => [task.id, task]));
  const spansById = new Map((spans ?? []).map((span) => [span.id, span]));

  if (collision.type === "span_span") {
    const first = spansById.get(collision.item1_id);
    const second = spansById.get(collision.item2_id);
    if (!first || !second) return null;
    return `${first.label} overlaps ${second.label}, ${formatCollisionRange(
      collision.overlaps_from,
      collision.overlaps_to,
    )}`;
  }

  if (collision.type === "task_span") {
    const task = tasksById.get(collision.item1_id);
    const span = spansById.get(collision.item2_id);
    if (!task || !span) return null;
    return `${task.title} is due during ${span.label}`;
  }

  return null;
}

export function titleCaseName(value) {
  if (!value) return "Viewer";
  return value.charAt(0).toUpperCase() + value.slice(1);
}
