<script>
  import "$lib/private/dashboard-theme.css";
  import { periodForHour } from "$lib/private/period.js";

  let { data } = $props();
  let hour = $state(new Date().getHours());
  let period = $derived(periodForHour(hour));

  // ── Formatting helpers ───────────────────────

  function shortSha(sha) {
    return typeof sha === "string" && sha.length > 0 ? sha.slice(0, 8) : "";
  }

  function commitUrl(sha) {
    return `https://github.com/jomcgi/homelab/commit/${sha}`;
  }

  // scan_ref looks like "refs/pull/3392/merge" for PR scans. Pull the PR
  // number out so we can render a linkable chip alongside the commit.
  function prNumber(scanRef) {
    const m = /^refs\/pull\/(\d+)\/merge$/.exec(scanRef ?? "");
    return m ? m[1] : null;
  }

  function prUrl(n) {
    return `https://github.com/jomcgi/homelab/pull/${n}`;
  }

  // Seconds under two minutes, minutes above (full scans run into the minutes).
  function formatDuration(seconds) {
    if (seconds == null) return null;
    if (seconds < 120) return `${seconds.toFixed(1)}s`;
    return `${(seconds / 60).toFixed(1)}m`;
  }

  function formatDate(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    const month = d.toLocaleString("en-US", { month: "short" });
    const day = d.getDate();
    const h = String(d.getHours()).padStart(2, "0");
    const min = String(d.getMinutes()).padStart(2, "0");
    return `${month} ${day} ${h}:${min}`;
  }

  function formatDay(iso) {
    if (!iso) return null;
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return null;
    return `${d.toLocaleString("en-US", { month: "short" })} ${d.getDate()}`;
  }

  // Best-available completion date across both sides for the Date column.
  function rowDate(row) {
    return (
      formatDate(row.route_b?.scan_completed_at) ??
      formatDate(row.sms?.scan_completed_at)
    );
  }

  function speedupLabel(speedup) {
    if (speedup == null) return null;
    if (speedup > 1.02)
      return { text: `${speedup.toFixed(1)}x faster`, tone: "ok" };
    if (speedup < 0.98)
      return { text: `${(1 / speedup).toFixed(1)}x slower`, tone: "bad" };
    return { text: "about even", tone: "neutral" };
  }

  // Build an inline SVG line chart of the rolling speedup trend. Returns null
  // when there are fewer than two points (a single point is not a line). Y is
  // auto-scaled to the data range so the shape shows the trend, not the absolute
  // magnitude; the tooltip and axis labels carry the numbers.
  function buildTrendChart(trend) {
    const pts = (trend?.points ?? []).filter((p) => p.speedup != null);
    if (pts.length < 2) return null;
    const W = 900,
      H = 200,
      padL = 8,
      padR = 8,
      padT = 16,
      padB = 26;
    const vals = pts.map((p) => p.speedup);
    const ymin = Math.min(...vals);
    const ymax = Math.max(...vals);
    const yrange = ymax - ymin || 1;
    const n = pts.length;
    const x = (i) => padL + (W - padL - padR) * (i / (n - 1));
    const y = (v) => padT + (H - padT - padB) * (1 - (v - ymin) / yrange);
    const line = pts
      .map(
        (p, i) =>
          `${i === 0 ? "M" : "L"} ${x(i).toFixed(1)} ${y(p.speedup).toFixed(1)}`,
      )
      .join(" ");
    const area = `${line} L ${x(n - 1).toFixed(1)} ${H - padB} L ${x(0).toFixed(1)} ${H - padB} Z`;
    return {
      W,
      H,
      line,
      area,
      dots: pts.map((p, i) => ({ cx: x(i), cy: y(p.speedup), ...p })),
      ymin,
      ymax,
      first: pts[0],
      latest: pts[n - 1],
    };
  }

  // ── View state ───────────────────────────────

  const buckets = [
    { key: "pr", label: "Pull request scans" },
    { key: "full", label: "Full scans (main)" },
  ];

  // The window has opened once at least one homelab scan exists. Before that
  // every managed scan is pre-homelab and excluded, so there is nothing valid
  // to compare and the aggregates are empty.
  let windowOpen = $derived(
    !data.error && (data.counts?.homelab ?? 0) > 0 && data.aggregates != null,
  );

  // The table defaults to matched pairs (both sides scanned the same work);
  // one-sided rows (a scan with no counterpart yet) are the historical backlog
  // and can be revealed with the toggle.
  let showOneSided = $state(false);
  let matchedComparisons = $derived(
    data.comparisons.filter((c) => c.route_b && c.sms),
  );
  let visibleComparisons = $derived(
    showOneSided ? data.comparisons : matchedComparisons,
  );
</script>

<svelte:head><title>Scan perf · private.jomcgi.dev</title></svelte:head>

<div class="shell {period}">
  <div class="dash">
    <header class="masthead">
      <h1 class="greeting">
        Semgrep scan performance<span class="greeting-mark">.</span>
      </h1>
      <p class="masthead-lead">
        homelab (self-hosted) vs Semgrep managed scans
      </p>
      {#if data.counts}
        <p class="masthead-sub">
          <span class="mono">{data.counts.homelab ?? 0}</span> homelab
          <span class="sub-sep">&middot;</span>
          <span class="mono">{data.counts.managed ?? 0}</span>
          managed{#if data.windowStart}
            <span class="sub-sep">&middot;</span> since {formatDay(
              data.windowStart,
            )}{/if}
        </p>
      {/if}
    </header>

    {#if data.error}
      <p class="unavail">perf data unavailable</p>
    {:else if !windowOpen}
      <section class="card card--empty">
        <h2 class="section-label">Waiting for the first homelab scan</h2>
        <p class="unavail">Comparisons appear after the first homelab scan.</p>
      </section>
    {:else}
      <section class="agg-grid">
        {#each buckets as bucket}
          {@const agg = data.aggregates[bucket.key] ?? { pairs: 0 }}
          {@const speedup = speedupLabel(agg.speedup)}
          <div class="card agg-card">
            <h2 class="section-label">{bucket.label}</h2>
            {#if agg.pairs > 0}
              <div
                class="agg-headline"
                class:agg-headline--ok={speedup?.tone === "ok"}
                class:agg-headline--bad={speedup?.tone === "bad"}
              >
                {speedup ? speedup.text : "-"}
              </div>
              <div class="agg-medians">
                <div class="agg-median">
                  <span class="agg-median-label">homelab median</span>
                  <span class="mono agg-median-value"
                    >{formatDuration(agg.homelab_median)}</span
                  >
                </div>
                <div class="agg-median-vs">vs</div>
                <div class="agg-median">
                  <span class="agg-median-label">managed median</span>
                  <span class="mono agg-median-value"
                    >{formatDuration(agg.managed_median)}</span
                  >
                </div>
              </div>
              <p class="agg-foot">
                {agg.pairs} matched pair{agg.pairs === 1
                  ? ""
                  : "s"}{#if agg.findings_pairs > 0}
                  <span class="sub-sep">&middot;</span> findings agree on {agg.findings_agree}/{agg.findings_pairs}{/if}
              </p>
            {:else}
              <p class="unavail agg-empty">No matched pairs yet</p>
            {/if}
            {#if data.distributions?.[bucket.key]}
              {@const dist = data.distributions[bucket.key]}
              <table class="dist">
                <thead>
                  <tr>
                    <th class="dist-side">all scans</th>
                    <th class="num">n</th>
                    <th class="num">p50</th>
                    <th class="num">p90</th>
                    <th class="num">max</th>
                  </tr>
                </thead>
                <tbody>
                  {#each [["homelab", dist.homelab], ["managed", dist.managed]] as [side, d]}
                    <tr>
                      <td class="dist-side">{side}</td>
                      <td class="num mono">{d.n}</td>
                      <td class="num mono">{formatDuration(d.p50) ?? "-"}</td>
                      <td class="num mono">{formatDuration(d.p90) ?? "-"}</td>
                      <td class="num mono">{formatDuration(d.max) ?? "-"}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            {/if}
          </div>
        {/each}
      </section>

      {#if data.trend && data.trend.points && data.trend.points.length >= 2}
        {@const chart = buildTrendChart(data.trend)}
        {#if chart}
          <section class="card trend">
            <div class="trend-head">
              <h2 class="section-label">Speedup trend</h2>
              <span class="trend-meta"
                >{data.trend.window_days}-day rolling &middot; latest
                <span class="mono">{chart.latest.speedup.toFixed(1)}x</span>
                (was
                <span class="mono">{chart.first.speedup.toFixed(1)}x</span
                >)</span
              >
            </div>
            <svg
              class="trend-svg"
              viewBox="0 0 {chart.W} {chart.H}"
              role="img"
              aria-label="Rolling speedup over time"
            >
              <path class="trend-area" d={chart.area} />
              <path class="trend-line" d={chart.line} />
              {#each chart.dots as d}
                <circle class="trend-dot" cx={d.cx} cy={d.cy} r="2.5">
                  <title
                    >{formatDay(d.date)}: {d.speedup.toFixed(1)}x &middot;
                    homelab {formatDuration(d.homelab_median)} vs managed {formatDuration(
                      d.managed_median,
                    )} &middot; {d.pairs}
                    pairs</title
                  >
                </circle>
              {/each}
              <text class="trend-axis" x={chart.W - 4} y="14" text-anchor="end"
                >{chart.ymax.toFixed(0)}x</text
              >
              <text
                class="trend-axis"
                x={chart.W - 4}
                y={chart.H - 30}
                text-anchor="end">{chart.ymin.toFixed(0)}x</text
              >
              <text class="trend-axis" x="8" y={chart.H - 8}
                >{formatDay(chart.first.date)}</text
              >
              <text
                class="trend-axis"
                x={chart.W - 8}
                y={chart.H - 8}
                text-anchor="end">{formatDay(chart.latest.date)}</text
              >
            </svg>
            <p class="trend-note">
              Higher is a wider lead. A drop means our advantage is narrowing
              &mdash; hover a point to see whether homelab slowed or managed
              sped up.
            </p>
          </section>
        {/if}
      {/if}

      {#if data.cohorts && data.cohorts.total_pairs > 0}
        <section class="card cohorts">
          <h2 class="section-label">Speedup by diff cohort</h2>
          <p class="cohort-note">
            {data.cohorts.total_pairs} matched PR pair{data.cohorts
              .total_pairs === 1
              ? ""
              : "s"} segmented by diff shape &mdash; which cohorts are at parity vs
            a major speedup.
          </p>
          <div class="cohort-grid">
            {#each [["By changed files", data.cohorts.by_files], ["By changed lines", data.cohorts.by_lines], ["By language", data.cohorts.by_language]] as [title, groups]}
              {#if groups && groups.length}
                <div class="cohort-block">
                  <h3 class="cohort-title">{title}</h3>
                  <table class="cohort-table">
                    <thead>
                      <tr>
                        <th>cohort</th>
                        <th class="num">n</th>
                        <th class="num">homelab</th>
                        <th class="num">managed</th>
                        <th class="num">speedup</th>
                      </tr>
                    </thead>
                    <tbody>
                      {#each groups as g}
                        {@const sp = speedupLabel(g.speedup)}
                        <tr>
                          <td>{g.label}</td>
                          <td class="num mono">{g.pairs}</td>
                          <td class="num mono"
                            >{formatDuration(g.homelab_median) ?? "-"}</td
                          >
                          <td class="num mono"
                            >{formatDuration(g.managed_median) ?? "-"}</td
                          >
                          <td class="num mono">
                            {#if sp}
                              <span
                                class="speedup"
                                class:speedup--ok={sp.tone === "ok"}
                                class:speedup--bad={sp.tone === "bad"}
                                >{sp.text}</span
                              >
                            {:else}
                              <span class="dim">&ndash;</span>
                            {/if}
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/if}
            {/each}
          </div>
        </section>
      {/if}

      <details class="detail">
        <summary class="detail-summary">
          Individual comparisons ({matchedComparisons.length} matched)
        </summary>
        {#if data.comparisons.length > matchedComparisons.length}
          <label class="onesided-toggle">
            <input type="checkbox" bind:checked={showOneSided} />
            show one-sided ({data.comparisons.length -
              matchedComparisons.length}
            scans with no counterpart yet)
          </label>
        {/if}
        <div class="card card--table">
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Ref / Commit</th>
                  <th>Type</th>
                  <th class="num">Homelab</th>
                  <th class="num">Managed</th>
                  <th class="num">Speedup</th>
                  <th class="num">Findings</th>
                  <th>Match</th>
                  <th>Date</th>
                </tr>
              </thead>
              <tbody>
                {#each visibleComparisons as row}
                  {@const sha = shortSha(row.commit_sha)}
                  {@const pr = prNumber(row.scan_ref)}
                  {@const speedup = speedupLabel(row.speedup)}
                  <tr>
                    <td>
                      <div class="ref-cell">
                        {#if sha}
                          <a
                            class="mono ref-link"
                            href={commitUrl(row.commit_sha)}
                            target="_blank"
                            rel="noopener">{sha}</a
                          >
                        {:else}
                          <span class="mono">{row.scan_ref}</span>
                        {/if}
                        {#if pr}
                          <a
                            class="pr-chip"
                            href={prUrl(pr)}
                            target="_blank"
                            rel="noopener">PR #{pr}</a
                          >
                        {/if}
                      </div>
                    </td>
                    <td>
                      <span class="badge" class:badge--full={row.is_full_scan}>
                        {row.is_full_scan ? "full" : "PR"}
                      </span>
                    </td>
                    <td class="num mono">
                      {#if row.route_b}
                        {formatDuration(row.route_b.total_time)}
                      {:else}
                        <span class="dim">no counterpart</span>
                      {/if}
                    </td>
                    <td class="num mono">
                      {#if row.sms}
                        {formatDuration(row.sms.total_time)}
                      {:else}
                        <span class="dim">no counterpart</span>
                      {/if}
                    </td>
                    <td class="num mono">
                      {#if speedup}
                        <span
                          class="speedup"
                          class:speedup--ok={speedup.tone === "ok"}
                          class:speedup--bad={speedup.tone === "bad"}
                          >{speedup.text}</span
                        >
                      {:else}
                        <span class="dim">&ndash;</span>
                      {/if}
                    </td>
                    <td class="num mono">
                      <span class:dim={row.route_b?.findings_total == null}
                        >{row.route_b?.findings_total ?? "-"}</span
                      >
                      <span class="dim"> / </span>
                      <span class:dim={row.sms?.findings_total == null}
                        >{row.sms?.findings_total ?? "-"}</span
                      >
                    </td>
                    <td><span class="dim">{row.match_kind}</span></td>
                    <td class="mono">{rowDate(row) ?? "-"}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
          {#if visibleComparisons.length === 0}
            <p class="unavail">
              No matched pairs yet. Matches appear as homelab and managed both
              scan the same commit; toggle above to see one-sided scans.
            </p>
          {/if}
        </div>
      </details>
    {/if}
  </div>
</div>

<style>
  .shell {
    position: fixed;
    inset: 0;
    overflow-y: auto;
    font-family: var(--font-ui);
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink);
    background:
      radial-gradient(1100px 700px at 12% -12%, var(--glow-a), transparent 62%),
      radial-gradient(900px 620px at 102% -6%, var(--glow-b), transparent 58%),
      var(--paper);
    -webkit-font-smoothing: antialiased;
  }

  .dash {
    max-width: 1360px;
    margin: 0 auto;
    padding: 52px 44px 72px;
  }

  .masthead {
    margin-bottom: 24px;
  }

  .greeting {
    font-family: var(--font-display);
    font-optical-sizing: auto;
    font-size: 34px;
    font-weight: 420;
    letter-spacing: -0.015em;
    line-height: 1.08;
    margin: 0;
  }

  .greeting-mark {
    color: var(--accent);
  }

  .masthead-lead {
    margin: 6px 0 0;
    font-size: 14px;
    color: var(--ink-2);
  }

  .masthead-sub {
    margin: 8px 0 0;
    font-size: 13px;
    color: var(--ink-2);
  }

  .sub-sep {
    color: var(--ink-3);
    padding: 0 2px;
  }

  .card {
    padding: 20px 22px 22px;
    background: var(--card-bg);
    border: 1px solid var(--line);
    border-radius: var(--radius);
    box-shadow: 0 1px 2px rgba(20, 16, 8, 0.03);
  }

  .card--empty {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  /* ── Aggregate cards ── */
  .agg-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
    margin-bottom: 20px;
  }

  .agg-card {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .agg-headline {
    font-family: var(--font-display);
    font-size: 30px;
    font-weight: 460;
    letter-spacing: -0.01em;
    line-height: 1;
    color: var(--ink);
  }

  .agg-headline--ok {
    color: var(--ok);
  }

  .agg-headline--bad {
    color: var(--bad);
  }

  .agg-medians {
    display: flex;
    align-items: flex-end;
    gap: 16px;
  }

  .agg-median {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .agg-median-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--ink-2);
  }

  .agg-median-value {
    font-size: 18px;
    color: var(--ink);
  }

  .agg-median-vs {
    font-size: 12px;
    color: var(--ink-3);
    padding-bottom: 3px;
  }

  .agg-foot {
    margin: 0;
    font-size: 12px;
    color: var(--ink-2);
  }

  .agg-empty {
    margin: 0;
  }

  /* Compact all-scans distribution table inside the aggregate cards. Cell
     rules are scoped to beat the shared comparison-table selectors below. */
  .dist {
    width: auto;
    margin-top: 4px;
    border-top: 1px solid var(--line);
  }

  .dist thead th {
    padding: 10px 14px 4px 0;
    border-bottom: none;
    letter-spacing: 0.1em;
  }

  .dist tbody td {
    padding: 2px 14px 2px 0;
    border-bottom: none;
    white-space: nowrap;
  }

  .dist .dist-side {
    text-align: left;
    font-size: 12px;
    color: var(--ink-2);
  }

  /* ── Collapsed detail ── */
  .detail {
    margin-top: 4px;
  }

  .detail-summary {
    cursor: pointer;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--ink-3);
    padding: 6px 0;
    user-select: none;
  }

  .detail-summary:hover {
    color: var(--ink-2);
  }

  .detail[open] .detail-summary {
    margin-bottom: 10px;
  }

  .onesided-toggle {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--ink-3);
    margin: 0 0 10px 2px;
    cursor: pointer;
    user-select: none;
  }

  .onesided-toggle input {
    accent-color: var(--accent);
    cursor: pointer;
  }

  .section-label {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.16em;
    color: var(--ink-3);
    margin: 0;
  }

  .unavail {
    font-size: 13px;
    color: var(--ink-3);
    margin: 0;
    font-style: italic;
  }

  .dim {
    color: var(--ink-3);
  }

  .mono {
    font-family: var(--font-code);
    letter-spacing: 0.08em;
    font-size: 13px;
  }

  .table-wrap {
    overflow-x: auto;
  }

  table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
  }

  thead th {
    text-align: left;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--ink-3);
    padding: 0 12px 8px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }

  th.num,
  td.num {
    text-align: right;
  }

  tbody td {
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
    color: var(--ink);
  }

  tbody tr:last-child td {
    border-bottom: none;
  }

  .ref-cell {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .ref-link {
    color: var(--accent);
    text-decoration: none;
  }

  .ref-link:hover {
    text-decoration: underline;
  }

  .pr-chip {
    font-size: 11px;
    font-weight: 600;
    color: var(--ink-2);
    background: color-mix(in srgb, var(--ink) 6%, var(--card-bg));
    border: 1px solid var(--line);
    border-radius: 999px;
    padding: 2px 8px;
    text-decoration: none;
    white-space: nowrap;
  }

  .pr-chip:hover {
    color: var(--ink);
  }

  .badge {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-2);
    background: color-mix(in srgb, var(--ink) 6%, var(--card-bg));
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 2px 6px;
  }

  .badge--full {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 35%, var(--line));
  }

  .speedup--ok {
    color: var(--ok);
  }

  .speedup--bad {
    color: var(--bad);
  }

  /* ── Cohort segmentation ── */
  .cohorts {
    margin-bottom: 20px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .cohort-note {
    margin: 0 0 8px;
    font-size: 12px;
    color: var(--ink-2);
  }

  .cohort-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 20px 28px;
  }

  .cohort-title {
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--ink-2);
    margin: 0 0 6px;
  }

  .cohort-table {
    width: 100%;
  }

  .cohort-table thead th {
    padding: 0 12px 6px 0;
    border-bottom: 1px solid var(--line);
  }

  .cohort-table tbody td {
    padding: 5px 12px 5px 0;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }

  .cohort-table tbody tr:last-child td {
    border-bottom: none;
  }

  /* ── Speedup trend chart ── */
  .trend {
    margin-bottom: 20px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .trend-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  .trend-meta {
    font-size: 12px;
    color: var(--ink-2);
  }

  .trend-svg {
    width: 100%;
    height: auto;
    display: block;
    overflow: visible;
  }

  .trend-area {
    fill: color-mix(in srgb, var(--accent) 12%, transparent);
    stroke: none;
  }

  .trend-line {
    fill: none;
    stroke: var(--accent);
    stroke-width: 2;
    stroke-linejoin: round;
    stroke-linecap: round;
    vector-effect: non-scaling-stroke;
  }

  .trend-dot {
    fill: var(--accent);
    stroke: var(--card-bg);
    stroke-width: 1.5;
  }

  .trend-axis {
    fill: var(--ink-3);
    font-family: var(--font-code);
    font-size: 11px;
  }

  .trend-note {
    margin: 0;
    font-size: 12px;
    color: var(--ink-3);
  }
</style>
