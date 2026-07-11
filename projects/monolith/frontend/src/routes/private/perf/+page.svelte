<script>
  import "$lib/private/dashboard-theme.css";

  let { data } = $props();

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
</script>

<svelte:head><title>Scan perf · private.jomcgi.dev</title></svelte:head>

<div class="shell day">
  <div class="dash">
    <header class="masthead">
      <h1 class="greeting">
        Semgrep scan performance<span class="greeting-mark">.</span>
      </h1>
      <p class="masthead-lead">homelab (self-hosted) vs Semgrep managed scans</p>
      {#if data.counts}
        <p class="masthead-sub">
          <span class="mono">{data.counts.homelab ?? 0}</span> homelab
          <span class="sub-sep">&middot;</span>
          <span class="mono">{data.counts.managed ?? 0}</span> managed{#if data.windowStart}
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
                {agg.pairs} matched pair{agg.pairs === 1 ? "" : "s"}
              </p>
            {:else}
              <p class="unavail agg-empty">No matched pairs yet</p>
            {/if}
          </div>
        {/each}
      </section>

      <details class="detail">
        <summary class="detail-summary">
          Individual comparisons ({data.comparisons.length})
        </summary>
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
                {#each data.comparisons as row}
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
</style>
