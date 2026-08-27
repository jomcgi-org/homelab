<script>
  // Fixed-duration load test drain over the fc-invoke daemon for one
  // workload (semgrep or sandbox). Three peer views: Live (1s poll while
  // running), Summary (once status is "done"), and Receipts (a lazily
  // fetched, paginated table of individual scans with a row drill-down).
  // Every non-resource time shown here is the fc-invoke EXECUTION wall
  // (client wall minus the drain's own oversubscription queue): the load is
  // artificial, so queueing is a property of the test, not the daemon, and is
  // deliberately not displayed (raw latency/queue stay in demo.load_scan).
  // Field names below are quoted from the backend as read, not guessed:
  //   - demos/firecracker_api.py _load_run_rollup(): run_id, workload,
  //     status, elapsed_s, total_scans, errors, throughput_per_s,
  //     in_flight_estimate, latency_p50, latency_p95 (both exec wall),
  //     per_lang_counts, cpu_ms_mean, peak_rss_mib_mean, summary (set once
  //     status=="done")
  //   - demos/loadtest.py build_summary(): total_scans, errors, wall_s,
  //     throughput_per_s, latency_ms{p50,p95,max} (exec wall),
  //     per_scan_cpu_ms{p50,p95}, per_scan_peak_rss_mib{p50,p95}, per_lang,
  //     daemon{pod_cpu_m,pod_rss_mib,source}, node{cpu_m,rss_mib},
  //     extrapolation{per_node_throughput_per_s,scans_per_core_s,
  //     vm_seconds,note}
  //   - demos/firecracker_api.py _load_scans_page(): total, offset, limit,
  //     scans[]{id,seq,name,status,scan_ms,cpu_ms,peak_rss_mib,result_count}
  //   - demos/firecracker_api.py _load_scan_detail(): adds result, error
  //   - demos/loadtest.py DAEMON_CONCURRENCY=16, run duration_s=60 (see
  //     _dispatch_drain in firecracker_api.py)
  let { workload } = $props();

  // Sandbox executes scripts ("runs"); semgrep scans files ("scans").
  let noun = $derived(workload === "sandbox" ? "run" : "scan");
  let nounPlural = $derived(workload === "sandbox" ? "runs" : "scans");

  const RUN_DURATION_S = 60;
  const DAEMON_CONCURRENCY = 16;
  const POLL_MS = 1000;
  const HARD_TIMEOUT_MS = 150_000;
  const SCANS_PAGE_SIZE = 50;

  let runId = $state(null);
  let rollup = $state(null);
  let starting = $state(false);
  let startError = $state(null);
  let busyNotice = $state(false);
  let timedOut = $state(null);

  // "live" | "summary" | "receipts". Independent of poll state: the user can
  // flip to Receipts while a run is still live, and it will not be disturbed
  // by the 1s live poll.
  let view = $state("live");

  let pollHandle = null;
  let pollStart = 0;

  // Receipts state: fetched lazily, only on first open / explicit paginate /
  // row click. Never touched by the live poll.
  let scans = $state([]);
  let scansTotal = $state(0);
  let scansOffset = $state(0);
  let scansLoading = $state(false);
  let scansError = $state(null);
  let scansFetchedFor = $state(null); // run_id this page was fetched for
  let selectedScan = $state(null);
  let selectedScanLoading = $state(false);
  let selectedScanError = $state(null);

  async function postJson(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(
        data?.error || data?.detail || `HTTP ${res.status}`,
      );
      err.status = res.status;
      throw err;
    }
    return data;
  }

  async function getJson(path) {
    const res = await fetch(path);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data?.error || data?.detail || `HTTP ${res.status}`);
    }
    return data;
  }

  function stopPolling() {
    if (pollHandle) clearTimeout(pollHandle);
    pollHandle = null;
  }

  async function pollOnce(id) {
    try {
      const data = await getJson(`/api/demos/firecracker/load-test/${id}`);
      if (runId !== id) return; // run changed mid-flight
      rollup = data;
    } catch (e) {
      if (runId !== id) return;
      startError = e?.message ?? String(e);
      stopPolling();
      return;
    }

    if (rollup?.status !== "running") {
      stopPolling();
      return;
    }

    if (performance.now() - pollStart >= HARD_TIMEOUT_MS) {
      timedOut = id;
      stopPolling();
      return;
    }

    pollHandle = setTimeout(() => pollOnce(id), POLL_MS);
  }

  function beginPolling(id) {
    stopPolling();
    pollStart = performance.now();
    timedOut = null;
    pollOnce(id);
  }

  async function startRun() {
    if (starting || rollup?.status === "running") return;
    starting = true;
    startError = null;
    busyNotice = false;
    try {
      const data = await postJson(
        `/api/demos/firecracker/load-test/${workload}`,
        {},
      );
      runId = data.run_id;
      // already_running is not an error: adopt the existing run and poll it.
      resetReceipts();
      view = "live";
      beginPolling(runId);
    } catch (e) {
      if (e?.status === 409) {
        busyNotice = true;
      } else {
        startError = e?.message ?? String(e);
      }
    } finally {
      starting = false;
    }
  }

  function resetReceipts() {
    scans = [];
    scansTotal = 0;
    scansOffset = 0;
    scansFetchedFor = null;
    scansError = null;
    selectedScan = null;
    selectedScanError = null;
  }

  async function openReceipts() {
    view = "receipts";
    if (scansFetchedFor !== runId) {
      await fetchScansPage(0);
    }
  }

  async function fetchScansPage(offset) {
    if (!runId) return;
    scansLoading = true;
    scansError = null;
    try {
      const data = await getJson(
        `/api/demos/firecracker/load-test/${runId}/scans?offset=${offset}&limit=${SCANS_PAGE_SIZE}`,
      );
      scans = data.scans ?? [];
      scansTotal = data.total ?? 0;
      scansOffset = data.offset ?? offset;
      scansFetchedFor = runId;
    } catch (e) {
      scansError = e?.message ?? String(e);
    } finally {
      scansLoading = false;
    }
  }

  function nextPage() {
    if (scansOffset + SCANS_PAGE_SIZE >= scansTotal) return;
    fetchScansPage(scansOffset + SCANS_PAGE_SIZE);
  }

  function prevPage() {
    if (scansOffset <= 0) return;
    fetchScansPage(Math.max(0, scansOffset - SCANS_PAGE_SIZE));
  }

  async function selectScan(id) {
    selectedScan = null;
    selectedScanError = null;
    selectedScanLoading = true;
    try {
      selectedScan = await getJson(
        `/api/demos/firecracker/load-test/${runId}/scans/${id}`,
      );
    } catch (e) {
      selectedScanError = e?.message ?? String(e);
    } finally {
      selectedScanLoading = false;
    }
  }

  function severityClass(sev) {
    const s = (sev ?? "").toLowerCase();
    if (s === "error" || s === "high" || s === "critical") return "sev--high";
    if (s === "warning" || s === "warn" || s === "medium") return "sev--medium";
    return "sev--low";
  }

  $effect(() => {
    return () => stopPolling();
  });

  let isRunning = $derived(rollup?.status === "running");
  let isDone = $derived(rollup?.status === "done");
  let hasScans = $derived((rollup?.total_scans ?? 0) > 0);
  let progressPct = $derived.by(() => {
    const elapsed = rollup?.elapsed_s ?? 0;
    return Math.max(0, Math.min(100, (elapsed / RUN_DURATION_S) * 100));
  });
  let scansPage = $derived(
    scansTotal > 0 ? Math.floor(scansOffset / SCANS_PAGE_SIZE) + 1 : 0,
  );
  let scansPageCount = $derived(
    Math.max(1, Math.ceil(scansTotal / SCANS_PAGE_SIZE)),
  );

  function fmt(n, digits = 1) {
    return n == null ? "-" : Number(n).toFixed(digits);
  }
</script>

<div class="load-test">
  <div class="lt-header">
    <p class="lt-blurb">
      Fixed 60 second drain against the fc-invoke daemon at concurrency
      {DAEMON_CONCURRENCY}, oversubscribed by the client so the daemon stays
      saturated for the whole run.
    </p>
    <button
      class="run-button"
      class:run-button--running={isRunning}
      onclick={startRun}
      disabled={starting || isRunning}
    >
      {#if starting}
        <span class="spinner" aria-hidden="true"></span> Starting
      {:else if isRunning}
        <span class="spinner" aria-hidden="true"></span> Running
      {:else}
        Start load test
      {/if}
    </button>
  </div>

  {#if busyNotice}
    <div class="notice notice--warn" role="alert">
      An agent run is active, try again shortly.
    </div>
  {/if}

  {#if startError}
    <div class="notice notice--error" role="alert">{startError}</div>
  {/if}

  {#if timedOut}
    <div class="notice notice--warn" role="alert">
      Stopped polling after 150s without the run finishing. It may still be
      running server-side; reopen the page to check again.
    </div>
  {/if}

  {#if rollup}
    <nav class="lt-tabs" aria-label="Load test view">
      <button
        type="button"
        class="lt-tab"
        class:active={view === "live"}
        onclick={() => (view = "live")}
      >
        Live
      </button>
      <button
        type="button"
        class="lt-tab"
        class:active={view === "summary"}
        onclick={() => (view = "summary")}
        disabled={!isDone}
      >
        Summary
      </button>
      {#if hasScans}
        <button
          type="button"
          class="lt-tab"
          class:active={view === "receipts"}
          onclick={openReceipts}
        >
          See Receipts
        </button>
      {/if}
    </nav>

    {#if view === "live"}
      <div class="lt-live">
        <div class="throughput-hero">
          <span class="throughput-value">{fmt(rollup.throughput_per_s, 2)}</span
          >
          <span class="throughput-unit">{nounPlural}/s</span>
        </div>

        <div class="progress-track" aria-hidden="true">
          <div class="progress-fill" style={`width: ${progressPct}%;`}></div>
        </div>
        <div class="progress-label">
          <!-- elapsed_s is the run row's wall clock (finished_at - started_at),
               which includes the drain grace + finalize, so it can exceed the
               configured duration; cap the display like the bar above. -->
          {fmt(Math.min(rollup.elapsed_s ?? 0, RUN_DURATION_S), 0)}s / {RUN_DURATION_S}s
          {#if isRunning}<span class="pulse-dot" aria-hidden="true"></span>{/if}
        </div>

        <div class="stat-grid">
          <div class="stat">
            <span class="stat-label">total {nounPlural}</span>
            <span class="stat-value">{rollup.total_scans ?? 0}</span>
          </div>
          <div class="stat">
            <span class="stat-label">in flight (est.)</span>
            <span class="stat-value">{rollup.in_flight_estimate ?? 0}</span>
          </div>
          <div class="stat">
            <span class="stat-label">errors</span>
            <span
              class="stat-value"
              class:stat-value--bad={(rollup.errors ?? 0) > 0}
              >{rollup.errors ?? 0}</span
            >
          </div>
          <div class="stat">
            <span class="stat-label">{noun} time p50</span>
            <span class="stat-value">{fmt(rollup.latency_p50, 0)}ms</span>
          </div>
          <div class="stat">
            <span class="stat-label">{noun} time p95</span>
            <span class="stat-value">{fmt(rollup.latency_p95, 0)}ms</span>
          </div>
          <div class="stat">
            <span class="stat-label">mean cpu</span>
            <span class="stat-value">{fmt(rollup.cpu_ms_mean, 0)}ms</span>
          </div>
          <div class="stat">
            <span class="stat-label">mean peak rss</span>
            <span class="stat-value"
              >{fmt(rollup.peak_rss_mib_mean, 0)} MiB</span
            >
          </div>
        </div>

        {#if rollup.per_lang_counts && Object.keys(rollup.per_lang_counts).length > 0}
          <div class="lang-rows">
            {#each Object.entries(rollup.per_lang_counts) as [name, count]}
              <div class="lang-row">
                <span class="lang-name">{name}</span>
                <span class="lang-count">{count}</span>
              </div>
            {/each}
          </div>
        {/if}
      </div>
    {:else if view === "summary"}
      {#if rollup.summary}
        {@const s = rollup.summary}
        <div class="lt-summary">
          <div class="extrap-card">
            <span class="extrap-label">extrapolation</span>
            <div class="extrap-figures">
              <div class="extrap-figure">
                <span class="extrap-value"
                  >{fmt(s.extrapolation?.per_node_throughput_per_s, 2)}</span
                >
                <span class="extrap-unit">{nounPlural}/s per node</span>
              </div>
              <div class="extrap-figure">
                <span class="extrap-value"
                  >{fmt(s.extrapolation?.scans_per_core_s, 3)}</span
                >
                <span class="extrap-unit">{nounPlural}/core/s</span>
              </div>
            </div>
            <p class="extrap-note">{s.extrapolation?.note}</p>
          </div>

          <div class="summary-section">
            <span class="section-title">daemon + node footprint</span>
            <div class="stat-grid">
              <div class="stat">
                <span class="stat-label">daemon cpu (mean/max)</span>
                <span class="stat-value"
                  >{fmt(s.daemon?.pod_cpu_m?.mean, 0)} / {fmt(
                    s.daemon?.pod_cpu_m?.max,
                    0,
                  )}m</span
                >
              </div>
              <div class="stat">
                <span class="stat-label">daemon rss (mean/max)</span>
                <span class="stat-value"
                  >{fmt(s.daemon?.pod_rss_mib?.mean, 0)} / {fmt(
                    s.daemon?.pod_rss_mib?.max,
                    0,
                  )} MiB</span
                >
              </div>
              <div class="stat">
                <span class="stat-label">daemon source</span>
                <span class="stat-value">{s.daemon?.source ?? "-"}</span>
              </div>
              {#if s.node}
                <div class="stat">
                  <span class="stat-label">node cpu (mean/max)</span>
                  <span class="stat-value"
                    >{fmt(s.node?.cpu_m?.mean, 0)} / {fmt(
                      s.node?.cpu_m?.max,
                      0,
                    )}m</span
                  >
                </div>
                <div class="stat">
                  <span class="stat-label">node rss (mean/max)</span>
                  <span class="stat-value"
                    >{fmt(s.node?.rss_mib?.mean, 0)} / {fmt(
                      s.node?.rss_mib?.max,
                      0,
                    )} MiB</span
                  >
                </div>
              {/if}
            </div>
          </div>

          <div class="summary-section">
            <span class="section-title">{noun} time + per-{noun} resources</span
            >
            <div class="stat-grid">
              <div class="stat">
                <span class="stat-label">{noun} time p50/p95/max</span>
                <span class="stat-value"
                  >{fmt(s.latency_ms?.p50, 0)} / {fmt(s.latency_ms?.p95, 0)} / {fmt(
                    s.latency_ms?.max,
                    0,
                  )}ms</span
                >
              </div>
              <div class="stat">
                <span class="stat-label">per-{noun} cpu p50/p95</span>
                <span class="stat-value"
                  >{fmt(s.per_scan_cpu_ms?.p50, 0)} / {fmt(
                    s.per_scan_cpu_ms?.p95,
                    0,
                  )}ms</span
                >
              </div>
              <div class="stat">
                <span class="stat-label">per-{noun} peak rss p50/p95</span>
                <span class="stat-value"
                  >{fmt(s.per_scan_peak_rss_mib?.p50, 0)} / {fmt(
                    s.per_scan_peak_rss_mib?.p95,
                    0,
                  )} MiB</span
                >
              </div>
              {#if s.result_count}
                <div class="stat">
                  <span class="stat-label">result count p50/p95/max</span>
                  <span class="stat-value"
                    >{fmt(s.result_count?.p50, 1)} / {fmt(
                      s.result_count?.p95,
                      1,
                    )} / {fmt(s.result_count?.max, 0)}</span
                  >
                </div>
              {/if}
              {#if s.sandbox_exit}
                <div class="stat">
                  <span class="stat-label">exit 0 / nonzero</span>
                  <span class="stat-value"
                    >{s.sandbox_exit.ok_count ?? 0} / {s.sandbox_exit
                      .nonzero_count ?? 0}</span
                  >
                </div>
              {/if}
              <div class="stat">
                <span class="stat-label">total {nounPlural} / errors</span>
                <span class="stat-value"
                  >{s.total_scans ?? 0} / {s.errors ?? 0}</span
                >
              </div>
            </div>
          </div>

          {#if s.per_lang && Object.keys(s.per_lang).length > 0}
            <div class="summary-section">
              <span class="section-title">per language</span>
              <div class="lang-rows">
                {#each Object.entries(s.per_lang) as [name, stats]}
                  <div class="lang-row">
                    <span class="lang-name">{name}</span>
                    <span class="lang-count">{stats.count}</span>
                    <span class="lang-extra"
                      >p50 {fmt(stats.p50_ms, 0)}ms · cpu p50 {fmt(
                        stats.p50_cpu_ms,
                        0,
                      )}ms</span
                    >
                  </div>
                {/each}
              </div>
            </div>
          {/if}
        </div>
      {:else}
        <p class="result-empty">Summary is not ready yet.</p>
      {/if}
    {:else if view === "receipts"}
      <div class="lt-receipts">
        {#if selectedScan}
          <button
            type="button"
            class="back-link"
            onclick={() => (selectedScan = null)}
          >
            &larr; back to scans
          </button>
          {#if selectedScanLoading}
            <p class="result-empty">Loading...</p>
          {:else if selectedScanError}
            <div class="notice notice--error" role="alert">
              {selectedScanError}
            </div>
          {:else}
            <div class="scan-detail">
              <div class="result-grid">
                <span class="result-key">{noun}</span>
                <span class="result-val"
                  >#{selectedScan.seq} · {selectedScan.name}</span
                >
              </div>
              <div class="result-grid">
                <span class="result-key">status</span>
                <span
                  class="result-val"
                  class:result-val--bad={selectedScan.status === "error"}
                  >{selectedScan.status}</span
                >
              </div>
              <div class="result-grid">
                <span class="result-key">{noun} time / cpu / rss</span>
                <span class="result-val"
                  >{selectedScan.scan_ms ?? "-"}ms / {selectedScan.cpu_ms ??
                    "-"}ms / {selectedScan.peak_rss_mib ?? "-"} MiB</span
                >
              </div>

              {#if selectedScan.error}
                <div class="notice notice--error" role="alert">
                  {selectedScan.error}
                </div>
              {/if}

              {#if workload === "semgrep"}
                {#if selectedScan.result?.findings?.length}
                  <ul class="findings">
                    {#each selectedScan.result.findings as f}
                      <li class="finding">
                        <span class="finding-sev {severityClass(f.severity)}"
                          >{f.severity}</span
                        >
                        <span class="finding-loc"
                          >{f.path}:{f.line}{f.col ? `:${f.col}` : ""}</span
                        >
                        <span class="finding-rule">{f.rule_id}</span>
                        <span class="finding-msg">{f.message}</span>
                      </li>
                    {/each}
                  </ul>
                {:else}
                  <p class="result-empty">No findings.</p>
                {/if}
                {#if selectedScan.result?.errors?.length}
                  <div class="result-block">
                    <span class="body-label">scan errors</span>
                    <pre
                      class="body-text body-text--error">{selectedScan.result.errors.join(
                        "\n",
                      )}</pre>
                  </div>
                {/if}
              {:else}
                <div class="result-grid">
                  <span class="result-key">exit code</span>
                  <span
                    class="result-val"
                    class:result-val--bad={selectedScan.result?.exit_code !== 0}
                    >{selectedScan.result?.exit_code}</span
                  >
                  {#if selectedScan.result?.truncated}
                    <span class="row-tag">truncated</span>
                  {/if}
                </div>
                <div class="result-grid">
                  <span class="result-key">duration</span>
                  <span class="result-val"
                    >{selectedScan.result?.duration_ms ?? "-"}ms</span
                  >
                </div>
                {#if selectedScan.result?.stdout}
                  <div class="result-block">
                    <span class="body-label">stdout</span>
                    <pre class="body-text">{selectedScan.result.stdout}</pre>
                  </div>
                {/if}
                {#if selectedScan.result?.stderr}
                  <div class="result-block">
                    <span class="body-label">stderr</span>
                    <pre class="body-text body-text--error">{selectedScan.result
                        .stderr}</pre>
                  </div>
                {/if}
              {/if}
            </div>
          {/if}
        {:else}
          {#if scansLoading}
            <p class="result-empty">Loading scans...</p>
          {:else if scansError}
            <div class="notice notice--error" role="alert">{scansError}</div>
          {:else}
            <table class="scans-table">
              <thead>
                <tr>
                  <th>seq</th>
                  <th>name</th>
                  <th>status</th>
                  <th>{noun} time</th>
                  <th>cpu</th>
                  <th>peak rss</th>
                  <th>results</th>
                </tr>
              </thead>
              <tbody>
                {#each scans as scan (scan.id)}
                  <tr class="scan-row" onclick={() => selectScan(scan.id)}>
                    <td>{scan.seq}</td>
                    <td class="scan-name">{scan.name}</td>
                    <td>
                      <span class:result-val--bad={scan.status === "error"}
                        >{scan.status}</span
                      >
                    </td>
                    <td>{scan.scan_ms ?? "-"}ms</td>
                    <td>{scan.cpu_ms ?? "-"}ms</td>
                    <td>{scan.peak_rss_mib ?? "-"} MiB</td>
                    <td>{scan.result_count ?? "-"}</td>
                  </tr>
                {/each}
              </tbody>
            </table>

            <div class="pager">
              <button
                type="button"
                onclick={prevPage}
                disabled={scansOffset <= 0}
              >
                Prev
              </button>
              <span class="pager-label"
                >page {scansPage} of {scansPageCount}</span
              >
              <button
                type="button"
                onclick={nextPage}
                disabled={scansOffset + SCANS_PAGE_SIZE >= scansTotal}
              >
                Next
              </button>
            </div>
          {/if}
        {/if}
      </div>
    {/if}
  {/if}
</div>

<style>
  .load-test {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .lt-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 20px;
    flex-wrap: wrap;
  }

  .lt-blurb {
    font-size: 13px;
    line-height: 1.5;
    color: var(--text-dim);
    margin: 0;
    max-width: 32rem;
  }

  .run-button {
    font-family: inherit;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: #fff; /* nosemgrep: svelte-hardcoded-color-in-style */
    background: var(--accent);
    border: none;
    border-radius: 6px;
    padding: 9px 20px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
    transition: opacity 0.1s ease;
  }

  .run-button:hover:not(:disabled) {
    opacity: 0.9;
  }

  .run-button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .run-button--running {
    background: var(--text-dim);
  }

  .spinner {
    width: 11px;
    height: 11px;
    border: 2px solid rgba(255, 255, 255, 0.5);
    border-top-color: #fff; /* nosemgrep: svelte-hardcoded-color-in-style */
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  .notice {
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 500;
  }

  .notice--error {
    color: #fff; /* nosemgrep: svelte-hardcoded-color-in-style */
    background: var(--danger);
  }

  .notice--warn {
    color: var(--ink);
    background: color-mix(
      in srgb,
      var(--loadtest-highlight) 18%,
      var(--surface)
    );
    border: 1px solid
      color-mix(in srgb, var(--loadtest-highlight) 40%, var(--line));
  }

  .lt-tabs {
    display: flex;
    gap: 4px;
    border-bottom: 1px solid var(--line);
  }

  .lt-tab {
    font-family: inherit;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--text-faint);
    background: transparent;
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 12px;
    margin-bottom: -1px;
    cursor: pointer;
  }

  .lt-tab:hover:not(:disabled) {
    color: var(--text-dim);
  }

  .lt-tab:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .lt-tab.active {
    color: var(--ink);
    border-bottom-color: var(--accent);
  }

  .lt-live,
  .lt-summary,
  .lt-receipts {
    display: flex;
    flex-direction: column;
    gap: 16px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface);
    padding: 18px 20px;
  }

  .throughput-hero {
    display: flex;
    align-items: baseline;
    gap: 8px;
  }

  .throughput-value {
    font-size: 44px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    line-height: 1;
  }

  .throughput-unit {
    font-size: 13px;
    font-weight: 600;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  .progress-track {
    height: 8px;
    border-radius: 999px;
    background: var(--paper);
    border: 1px solid var(--line);
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--loadtest-progress);
    transition: width 0.3s ease;
  }

  .progress-label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 11px;
    font-variant-numeric: tabular-nums;
    color: var(--text-faint);
  }

  .pulse-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--accent);
    animation: pulse 1.2s ease-in-out infinite;
    flex-shrink: 0;
  }

  @keyframes pulse {
    0%,
    100% {
      opacity: 0.25;
      transform: scale(0.85);
    }
    50% {
      opacity: 1;
      transform: scale(1);
    }
  }

  .stat-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(11rem, 1fr));
    gap: 12px 20px;
  }

  .stat {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .stat-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-faint);
  }

  .stat-value {
    font-size: 15px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }

  .stat-value--bad {
    color: var(--danger);
  }

  .lang-rows {
    display: flex;
    flex-direction: column;
    gap: 4px;
    border-top: 1px solid var(--line);
    padding-top: 10px;
  }

  .lang-row {
    display: flex;
    align-items: baseline;
    gap: 10px;
    font-size: 12px;
  }

  .lang-name {
    font-weight: 600;
    color: var(--ink);
    min-width: 6rem;
  }

  .lang-count {
    font-variant-numeric: tabular-nums;
    color: var(--text-dim);
  }

  .lang-extra {
    color: var(--text-faint);
    font-size: 11px;
  }

  .extrap-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    border: 1px solid
      color-mix(in srgb, var(--loadtest-highlight) 40%, var(--line));
    background: color-mix(
      in srgb,
      var(--loadtest-highlight) 10%,
      var(--surface)
    );
    border-radius: 8px;
    padding: 16px 18px;
  }

  .extrap-label {
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--loadtest-highlight);
  }

  .extrap-figures {
    display: flex;
    gap: 28px;
    flex-wrap: wrap;
  }

  .extrap-figure {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .extrap-value {
    font-size: 28px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    line-height: 1;
  }

  .extrap-unit {
    font-size: 11px;
    color: var(--text-faint);
  }

  .extrap-note {
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-dim);
    margin: 0;
  }

  .summary-section {
    display: flex;
    flex-direction: column;
    gap: 10px;
    border-top: 1px solid var(--line);
    padding-top: 14px;
  }

  .section-title {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-faint);
  }

  .result-empty {
    font-size: 13px;
    color: var(--text-faint);
    margin: 0;
  }

  .scans-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;
  }

  .scans-table th {
    text-align: left;
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-faint);
    padding: 6px 8px;
    border-bottom: 1px solid var(--line);
  }

  .scans-table td {
    padding: 6px 8px;
    border-bottom: 1px solid var(--line);
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }

  .scan-name {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-variant-numeric: normal;
  }

  .scan-row {
    cursor: pointer;
  }

  .scan-row:hover {
    background: var(--paper);
  }

  .pager {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .pager button {
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    color: var(--ink);
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 5px 12px;
    cursor: pointer;
  }

  .pager button:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .pager-label {
    font-size: 11px;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
  }

  .back-link {
    align-self: flex-start;
    font-family: inherit;
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    background: transparent;
    border: none;
    cursor: pointer;
    padding: 0;
  }

  .scan-detail {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .result-grid {
    display: flex;
    gap: 10px;
    align-items: baseline;
    font-size: 13px;
  }

  .result-key {
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 11px;
    color: var(--text-faint);
  }

  .result-val {
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }

  .result-val--bad {
    color: var(--danger);
    font-weight: 600;
  }

  .result-block {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .body-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-faint);
  }

  .body-text {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 12.5px;
    color: var(--ink);
    background: var(--paper);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 10px 12px;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 14rem;
    overflow-y: auto;
  }

  .body-text--error {
    color: var(--danger);
  }

  .row-tag {
    font-size: 9px;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border: 1px solid var(--line);
    border-radius: 3px;
    padding: 0 4px;
    white-space: nowrap;
  }

  .findings {
    display: flex;
    flex-direction: column;
    gap: 8px;
    max-height: 16rem;
    overflow-y: auto;
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .finding {
    display: grid;
    grid-template-columns: max-content max-content 1fr;
    gap: 6px 10px;
    align-items: baseline;
    font-size: 12.5px;
    padding: 8px 0;
    border-bottom: 1px solid var(--line);
  }

  .finding-sev {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 2px 6px;
    border-radius: 4px;
    border: 1px solid var(--line);
    grid-row: 1;
  }

  .sev--high {
    background: color-mix(in srgb, var(--danger) 14%, var(--surface));
    color: var(--danger);
    border-color: color-mix(in srgb, var(--danger) 35%, var(--line));
  }

  .sev--medium {
    background: color-mix(in srgb, var(--sev-medium) 12%, var(--surface));
    color: var(--sev-medium);
    border-color: color-mix(in srgb, var(--sev-medium) 30%, var(--line));
  }

  .sev--low {
    background: var(--paper);
    color: var(--text-dim);
  }

  .finding-loc {
    font-variant-numeric: tabular-nums;
    color: var(--text-dim);
    grid-row: 1;
  }

  .finding-rule {
    font-weight: 600;
    color: var(--text-faint);
    grid-column: 3;
    grid-row: 1;
  }

  .finding-msg {
    grid-column: 1 / -1;
    color: var(--ink);
  }

  @media (max-width: 640px) {
    .stat-grid {
      grid-template-columns: 1fr 1fr;
    }
    .extrap-figures {
      gap: 16px;
    }
    .scans-table {
      font-size: 11px;
    }
    .finding {
      grid-template-columns: max-content 1fr;
    }
    .finding-rule {
      grid-column: 1 / -1;
      grid-row: 2;
    }
    .finding-msg {
      grid-row: 3;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .pulse-dot,
    .spinner {
      animation: none;
    }
  }
</style>
