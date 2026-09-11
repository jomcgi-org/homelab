export const STATS_REFRESH_INTERVAL_MS = 5 * 60_000;
export const STATS_AGE_TICK_INTERVAL_MS = 30_000;

const STATS_SECTIONS = ["cluster", "gpu", "knowledge", "deploy"];

/** Build marquee items from /stats data, skipping any item whose source
 *  is unavailable so the ticker never shows fabricated numbers. */
export function buildMarquee(stats) {
  const items = ["~/homelab"];
  const c = stats?.cluster;
  const g = stats?.gpu;
  const k = stats?.knowledge;
  const d = stats?.deploy;

  if (c?.nodes != null && c?.pods != null)
    items.push(`${c.nodes} nodes · ${c.pods} pods`);
  if (c?.cpu_used_cores != null && c?.cpu_capacity_cores != null) {
    items.push(`cpu: ${c.cpu_used_cores} / ${c.cpu_capacity_cores} cores`);
  }
  if (c?.memory_used_gb != null && c?.memory_capacity_gb != null) {
    items.push(`mem: ${c.memory_used_gb} / ${c.memory_capacity_gb} gb`);
  }
  if (g?.utilization_pct != null) {
    const memPart =
      g?.memory_used_gb != null && g?.memory_total_gb != null
        ? ` · ${g.memory_used_gb} / ${g.memory_total_gb} gb`
        : "";
    items.push(`gpu: ${g.utilization_pct}%${memPart}`);
  }
  if (c?.argocd_apps != null) items.push(`argocd: ${c.argocd_apps} apps`);
  if (k?.facts != null) items.push(`kg: ${k.facts.toLocaleString()} notes`);
  if (d?.latest_commit_sha) items.push(`last commit: ${d.latest_commit_sha}`);
  if (d?.deployed_at) {
    const ago = formatAgo(d.deployed_at);
    if (ago) items.push(`deployed ${ago} ago`);
  }
  return items;
}

function formatAgo(iso) {
  const then = Date.parse(iso);
  if (!Number.isFinite(then)) return null;
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

export function isStatsSnapshot(value) {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }

  return STATS_SECTIONS.some((section) => {
    const data = value[section];
    return data !== null && typeof data === "object" && !Array.isArray(data);
  });
}

export function startHomepageStatsPolling(
  initialStats,
  render,
  {
    fetchFn = globalThis.fetch,
    schedule = globalThis.setInterval,
    cancel = globalThis.clearInterval,
    createAbortController = () => new AbortController(),
  } = {},
) {
  let currentStats = initialStats;
  let active = true;
  let inFlight = null;

  const redrawAge = () => {
    if (active) render(currentStats);
  };

  const refresh = async () => {
    if (!active || inFlight) return;

    const controller = createAbortController();
    inFlight = controller;
    try {
      const response = await fetchFn("/app/notes/stats", {
        signal: controller.signal,
      });
      if (!response.ok) return;

      const nextStats = await response.json();
      if (!active || inFlight !== controller || !isStatsSnapshot(nextStats)) {
        return;
      }

      currentStats = nextStats;
      render(currentStats);
    } catch {
      // Retain the most recent usable snapshot and retry on the next interval.
    } finally {
      if (inFlight === controller) inFlight = null;
    }
  };

  const refreshTimer = schedule(refresh, STATS_REFRESH_INTERVAL_MS);
  const ageTimer = schedule(redrawAge, STATS_AGE_TICK_INTERVAL_MS);

  return () => {
    active = false;
    cancel(refreshTimer);
    cancel(ageTimer);
    inFlight?.abort();
    inFlight = null;
  };
}
