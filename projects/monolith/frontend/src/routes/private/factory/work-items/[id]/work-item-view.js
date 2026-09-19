export function edgeRows(document) {
  if (!document) return {};

  const rows = {};
  const edges_in = document.edges_in || [];
  const edges_out = document.edges_out || [];
  const item = document.item || {};

  // Group edges by kind and direction
  // edges_in: other items block/parent/supersede this item
  // edges_out: this item blocks/parents/supersedes other items

  // For blocked_by: edges_in with kind="blocks"
  rows.blocked_by = edges_in.filter((e) => e.kind === "blocks");

  // For blocks: edges_out with kind="blocks"
  rows.blocks = edges_out.filter((e) => e.kind === "blocks");

  // For parent: edges_in with kind="parent" (other is parent of this)
  rows.parent = edges_in.filter((e) => e.kind === "parent");

  // For children: edges_out with kind="parent" (this is parent of other)
  rows.children = edges_out.filter((e) => e.kind === "parent");

  // For supersedes: edges_out with kind="supersedes" (this supersedes other)
  rows.supersedes = edges_out.filter((e) => e.kind === "supersedes");

  // For superseded_by: edges_in with kind="supersedes" (other supersedes this)
  rows.superseded_by = edges_in.filter((e) => e.kind === "supersedes");

  return rows;
}

export function eventLines(document, now) {
  if (!document || !document.events) return [];

  const events = document.events || [];
  return events.map((event) => {
    const createdAt = new Date(event.created_at).getTime();
    const relTime = relativeTime(createdAt, now);
    return {
      ...event,
      relativeTime: relTime,
    };
  });
}

export function parseOther(input) {
  input = (input || "").trim();
  if (!input) return null;

  if (input.startsWith("#")) {
    // Issue number format: #123
    const match = input.match(/^#(\d+)$/);
    if (match) return { type: "issue", number: parseInt(match[1], 10) };
    return null;
  }

  const num = parseInt(input, 10);
  if (!isNaN(num)) {
    return { type: "id", id: num };
  }

  return null;
}

export function relativeTime(timestamp, now) {
  const diffMs = now - timestamp;
  const diffS = Math.floor(diffMs / 1000);
  const diffM = Math.floor(diffS / 60);
  const diffH = Math.floor(diffM / 60);
  const diffD = Math.floor(diffH / 24);

  if (diffS < 60) return `${Math.max(0, diffS)}s ago`;
  if (diffM < 60) return `${diffM}m ago`;
  if (diffH < 24) return `${diffH}h ago`;
  if (diffD < 30) return `${diffD}d ago`;

  const date = new Date(timestamp);
  return date.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year:
      date.getFullYear() !== new Date().getFullYear() ? "numeric" : undefined,
  });
}

export function directionLabel(kind, direction) {
  const labels = {
    blocks: {
      out: "this blocks",
      in: "this is blocked by",
    },
    parent: {
      out: "parent of",
      in: "child of",
    },
    supersedes: {
      out: "supersedes",
      in: "superseded by",
    },
  };
  return labels[kind]?.[direction] || `${kind} ${direction}`;
}
