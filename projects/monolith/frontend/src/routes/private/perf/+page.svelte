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

  function formatTime(seconds) {
    if (seconds == null) return null;
    return `${seconds.toFixed(1)}s`;
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

  // Best-available completion date across both sides for the Date column.
  function rowDate(row) {
    return (
      formatDate(row.route_b?.scan_completed_at) ??
      formatDate(row.sms?.scan_completed_at)
    );
  }

  function speedupLabel(speedup) {
    if (speedup == null) return null;
    if (speedup > 1.02) return { text: `${speedup.toFixed(1)}x faster`, tone: "ok" };
    if (speedup < 0.98) return { text: `${(1 / speedup).toFixed(1)}x slower`, tone: "bad" };
    return { text: "even", tone: "neutral" };
  }

  function findingsLabel(row) {
    const rb = row.route_b?.findings_total;
    const sms = row.sms?.findings_total;
    return { rb: rb ?? null, sms: sms ?? null };
  }
</script>

<svelte:head><title>Scan perf · private.jomcgi.dev</title></svelte:head>

<div class="shell day">
  <div class="dash">
    <header class="masthead">
      <div class="masthead-words">
        <h1 class="greeting">Semgrep scan performance<span class="greeting-mark">.</span></h1>
        {#if data.counts}
          <p class="masthead-sub">
            {data.counts.route_b ?? 0} Route B scans, {data.counts.sms ?? 0} managed scans
          </p>
        {/if}
        {#if data.note}
          <p class="masthead-note">{data.note}</p>
        {/if}
      </div>
    </header>

    {#if data.error}
      <p class="unavail">perf data unavailable</p>
    {/if}

    {#if !data.error && data.comparisons.length === 0}
      <section class="card card--empty">
        <h2 class="section-label">No comparison data yet</h2>
        <p class="unavail">
          Comparisons populate over time as Route B and Semgrep Managed Scans
          complete against the same commits.
        </p>
      </section>
    {:else if data.comparisons.length > 0}
      <section class="card card--table">
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Ref / Commit</th>
                <th>Type</th>
                <th class="num">Route B</th>
                <th class="num">SMS</th>
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
                {@const findings = findingsLabel(row)}
                <tr>
                  <td>
                    <div class="ref-cell">
                      {#if sha}
                        <a
                          class="mono ref-link"
                          href={commitUrl(row.commit_sha)}
                          target="_blank"
                          rel="noopener"
                        >{sha}</a>
                      {:else}
                        <span class="mono">{row.scan_ref}</span>
                      {/if}
                      {#if pr}
                        <a
                          class="pr-chip"
                          href={prUrl(pr)}
                          target="_blank"
                          rel="noopener"
                        >PR #{pr}</a>
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
                      {formatTime(row.route_b.total_time)}
                    {:else}
                      <span class="dim">no counterpart</span>
                    {/if}
                  </td>
                  <td class="num mono">
                    {#if row.sms}
                      {formatTime(row.sms.total_time)}
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
                      >{speedup.text}</span>
                    {:else}
                      <span class="dim">&ndash;</span>
                    {/if}
                  </td>
                  <td class="num mono">
                    <span class:dim={findings.rb == null}>{findings.rb ?? "-"}</span>
                    <span class="dim"> / </span>
                    <span class:dim={findings.sms == null}>{findings.sms ?? "-"}</span>
                  </td>
                  <td><span class="dim">{row.match_kind}</span></td>
                  <td class="mono">{rowDate(row) ?? "-"}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </section>
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

  .masthead-sub {
    margin: 8px 0 0;
    font-size: 14px;
    color: var(--ink-2);
  }

  .masthead-note {
    margin: 4px 0 0;
    font-size: 12px;
    color: var(--ink-3);
    max-width: 720px;
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
