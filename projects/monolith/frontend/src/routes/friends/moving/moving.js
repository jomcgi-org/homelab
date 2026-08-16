const DAY_MS = 24 * 60 * 60 * 1000;

const MONTH_SHORT = [
  "Jan",
  "Feb",
  "Mar",
  "Apr",
  "May",
  "Jun",
  "Jul",
  "Aug",
  "Sep",
  "Oct",
  "Nov",
  "Dec",
];

const SPAN_KIND_LABELS = {
  visitor: "Visitors",
  work: "Work trips",
  move: "The move",
  trip: "Trips",
};

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

function dateValue(now) {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function todayStamp(now) {
  return dateStamp(dateValue(now));
}

function formatDate(value, options) {
  const stamp = dateStamp(value);
  if (stamp == null) return "an unknown date";
  return new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    ...options,
  }).format(new Date(stamp));
}

export function formatShortDate(value) {
  const parts = dateParts(value);
  if (!parts) return "Date unknown";
  return `${parts.day} ${MONTH_SHORT[parts.month - 1]}`;
}

export function formatDateRange(from, to) {
  const start = dateParts(from);
  const end = dateParts(to);
  if (!start || !end) return "Dates unknown";
  if (from === to) return formatShortDate(from);
  if (start.year === end.year && start.month === end.month) {
    return `${start.day}\u2013${end.day} ${MONTH_SHORT[start.month - 1]}`;
  }
  return `${formatShortDate(from)} \u2013 ${formatShortDate(to)}`;
}

export function dayDistance(from, to) {
  const start = dateStamp(from);
  const end = dateStamp(to);
  if (start == null || end == null) return null;
  return Math.round((end - start) / DAY_MS);
}

export function moveCountdown(spans, now = new Date()) {
  const moves = (spans ?? [])
    .filter((span) => span.kind === "move" && dateStamp(span.starts_on) != null)
    .toSorted((left, right) => left.starts_on.localeCompare(right.starts_on));

  if (moves.length === 0) {
    return {
      headline: "No move date set",
      detail: "Add a move span when the date is known.",
      days: null,
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
    days,
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
    label: `${done} of ${total} tasks`,
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

export function taskDueView(value, now = new Date()) {
  const distance = dayDistance(dateValue(now), value);
  if (distance == null) {
    return { bucket: "later", label: "No due date", days: null };
  }
  if (distance < 0) {
    return {
      bucket: "overdue",
      label: `${Math.abs(distance)}d late`,
      days: distance,
    };
  }
  if (distance <= 7) {
    return {
      bucket: "week",
      label: distance === 0 ? "today" : `in ${distance}d`,
      days: distance,
    };
  }
  return { bucket: "later", label: formatShortDate(value), days: distance };
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
  if (!collision) return null;
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

export function ganttDatePosition(value, startsOn, endsOn) {
  const valueStamp = dateStamp(value);
  const startStamp = dateStamp(startsOn);
  const endStamp = dateStamp(endsOn);
  if (
    valueStamp == null ||
    startStamp == null ||
    endStamp == null ||
    endStamp <= startStamp
  ) {
    return null;
  }
  const percent = ((valueStamp - startStamp) / (endStamp - startStamp)) * 100;
  return Math.min(100, Math.max(0, percent));
}

export function ganttTimeline(spans, milestones, now = new Date()) {
  const today = dateValue(now);
  const values = [
    today,
    ...(spans ?? []).flatMap((span) => [span.starts_on, span.ends_on]),
    ...(milestones ?? []).map((milestone) => milestone.occurs_on),
  ].filter((value) => dateStamp(value) != null);
  const stamps = values.map(dateStamp);
  const first = new Date(Math.min(...stamps));
  const last = new Date(Math.max(...stamps));
  const start = new Date(
    Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), 1),
  );
  const end = new Date(
    Date.UTC(last.getUTCFullYear(), last.getUTCMonth() + 1, 0),
  );
  const startsOn = start.toISOString().slice(0, 10);
  const endsOn = end.toISOString().slice(0, 10);
  const months = [];
  for (
    let cursor = new Date(start);
    cursor <= end;
    cursor.setUTCMonth(cursor.getUTCMonth() + 1)
  ) {
    const value = cursor.toISOString().slice(0, 10);
    months.push({
      value,
      label: MONTH_SHORT[cursor.getUTCMonth()],
      position: ganttDatePosition(value, startsOn, endsOn),
    });
  }

  const lanes = Object.entries(SPAN_KIND_LABELS)
    .map(([kind, label]) => ({
      kind,
      label,
      bars: (spans ?? [])
        .filter(
          (span) =>
            span.kind === kind &&
            dateStamp(span.starts_on) != null &&
            dateStamp(span.ends_on) != null,
        )
        .map((span) => ({
          ...span,
          position: ganttDatePosition(span.starts_on, startsOn, endsOn),
          width: Math.max(
            0.8,
            ganttDatePosition(span.ends_on, startsOn, endsOn) -
              ganttDatePosition(span.starts_on, startsOn, endsOn),
          ),
        })),
    }))
    .filter((lane) => lane.bars.length > 0);

  return {
    startsOn,
    endsOn,
    months,
    lanes,
    todayPosition: ganttDatePosition(today, startsOn, endsOn),
  };
}

export function mergeAgendaItems(milestones, spans, collisions, tasks) {
  const items = [];
  for (const milestone of milestones ?? []) {
    if (dateStamp(milestone.occurs_on) == null) continue;
    items.push({
      id: `milestone-${milestone.id}`,
      date: milestone.occurs_on,
      kind: "ms",
      held: milestone.gcal_state === "held",
      icon: "◆",
      title: milestone.title,
      sub: `${titleCaseName(milestone.owner)} · ${milestone.gcal_state}`,
    });
  }
  for (const span of spans ?? []) {
    if (dateStamp(span.starts_on) == null) continue;
    items.push({
      id: `span-${span.id}`,
      date: span.starts_on,
      kind: "span",
      held: false,
      icon: "▬",
      title: span.label,
      sub: formatDateRange(span.starts_on, span.ends_on),
    });
  }
  for (const collision of collisions ?? []) {
    if (dateStamp(collision.overlaps_from) == null) continue;
    const title = collisionWording(collision, tasks, spans);
    if (!title) continue;
    items.push({
      id: `collision-${collision.type}-${collision.item1_id}-${collision.item2_id}`,
      date: collision.overlaps_from,
      kind: "col",
      held: false,
      icon: collision.type === "task_span" ? "▲" : "△",
      title,
      sub: "Date collision",
    });
  }
  return items
    .toSorted(
      (left, right) =>
        left.date.localeCompare(right.date) ||
        left.kind.localeCompare(right.kind),
    )
    .map((item) => ({
      ...item,
      monthKey: item.date.slice(0, 7),
      monthLabel: formatDate(item.date, { month: "long", year: "numeric" }),
    }));
}

export function sumSellValues(tasks) {
  return (tasks ?? [])
    .filter((task) => task.track === "sell")
    .reduce((total, task) => {
      const value = Number(task.value_cad);
      return total + (Number.isFinite(value) ? value : 0);
    }, 0);
}

export function formatCad(value) {
  const numeric = Number(value);
  const amount = Number.isFinite(numeric) ? numeric : 0;
  return `C$${amount.toLocaleString("en-CA", {
    minimumFractionDigits: Number.isInteger(amount) ? 0 : 2,
    maximumFractionDigits: 2,
  })}`;
}

export function titleCaseName(value) {
  if (!value) return "Viewer";
  return value.charAt(0).toUpperCase() + value.slice(1);
}
