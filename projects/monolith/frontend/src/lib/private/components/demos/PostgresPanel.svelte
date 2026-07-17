<script>
  // Demo-postgres: the embervm R4 stateful sleep/wake exhibit. A dedicated
  // scale-to-zero Postgres microVM tuned to bank (pause-to-disk) ~a second
  // after its last connection closes, and wake on the next TCP connect.
  //
  // Three moving parts, all backend-proxied:
  //   status poll -> GET  /api/demos/firecracker/postgres/status. A control-
  //                  plane management read: it never opens a connection to the
  //                  workload itself, so polling CANNOT keep the VM awake.
  //                  This is what lets the sleep indicator watch the VM doze
  //                  off in real time without heisenberging the demo.
  //   query       -> POST /api/demos/firecracker/postgres/query. The backend
  //                  psycopg-connects (short-lived by design: an open
  //                  connection pins the VM awake), runs the mode's
  //                  statements against an orders ledger, and returns two
  //                  separately-bracketed wall times: connect (which IS the
  //                  wake when asleep) and query, plus the verbatim SQL run.
  //   truncate    -> POST /api/demos/firecracker/postgres/truncate. Clears
  //                  the ledger; the VM lives, the data dies (the mirror of
  //                  reset, which destroys the VM and keeps the data).
  //   reset       -> POST /api/demos/firecracker/postgres/reset. Destroys the
  //                  live VM AND evicts its snapshot; the data volume
  //                  survives, so the next query pays a full cold boot against
  //                  retained rows: the durability proof on demand.
  import { onMount } from "svelte";

  const API = "/api/demos/firecracker/postgres";
  const POLL_MS = 700;
  const HISTORY_MAX = 8;

  const COLUMN_TYPES = [
    { key: "id", label: "id", type: "bigserial" },
    { key: "item", label: "item", type: "text" },
    { key: "qty", label: "qty", type: "int" },
    { key: "unit_price", label: "unit_price", type: "numeric(8,2)" },
    { key: "written_at", label: "written_at", type: "timestamptz" },
  ];

  let status = $state(null);
  let statusError = $state("");
  let running = $state(false);
  let resetting = $state(false);
  let truncating = $state(false);
  let truncateConfirming = $state(false);
  let lastRun = $state(null);
  let runs = $state([]);
  let runError = $state("");

  // Best-seen wall time per boot path: the demo's headline comparison.
  let tiers = $state({ cold: null, relight: null, warm: null });

  let pollTimer = null;

  // Live wake stopwatch: ticks via requestAnimationFrame while a query is
  // in flight, so the visitor watches the wake happen instead of just
  // seeing the final number appear.
  let stopwatchMs = $state(0);
  let stopwatchRaf = null;
  let stopwatchStart = 0;
  let connectPulse = $state(false);
  let pulseTimer = null;

  function startStopwatch() {
    stopwatchStart = performance.now();
    stopwatchMs = 0;
    const tick = () => {
      stopwatchMs = performance.now() - stopwatchStart;
      stopwatchRaf = requestAnimationFrame(tick);
    };
    stopwatchRaf = requestAnimationFrame(tick);
  }

  function stopStopwatch() {
    if (stopwatchRaf != null) {
      cancelAnimationFrame(stopwatchRaf);
      stopwatchRaf = null;
    }
  }

  function flashConnectPulse() {
    connectPulse = false;
    // Retrigger the CSS animation even if it fired moments ago.
    requestAnimationFrame(() => {
      connectPulse = true;
      if (pulseTimer) clearTimeout(pulseTimer);
      pulseTimer = setTimeout(() => {
        connectPulse = false;
      }, 500);
    });
  }

  // Falling-asleep narration: seconds since the VM's last activity, ticked
  // by the status poll (not a fabricated countdown, just a rough approach).
  let idleSeconds = $state(0);

  async function pollStatus() {
    try {
      const resp = await fetch(`${API}/status`);
      const body = await resp.json();
      if (body.configured === false) {
        statusError = "demo-postgres is not configured on this deployment";
        return;
      }
      if (body.error) {
        // One flaky poll is data, not an outage: keep the last good status.
        statusError = body.error;
        return;
      }
      statusError = "";
      status = body;
      idleSeconds =
        body.state === "serving" && body.last_active_at
          ? (Date.now() - new Date(body.last_active_at).getTime()) / 1000
          : 0;
    } catch (err) {
      statusError = String(err);
    }
  }

  async function runQuery(mode) {
    if (running) return;
    truncateConfirming = false;
    running = true;
    runError = "";
    startStopwatch();
    try {
      const resp = await fetch(`${API}/query`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      const body = await resp.json();
      if (!resp.ok) {
        runError = body?.detail || `query failed (${resp.status})`;
        return;
      }
      if (body.error) {
        runError = body.error;
        return;
      }
      lastRun = body;
      runs = [body, ...runs].slice(0, HISTORY_MAX);
      const tier =
        body.classification === "relight" || body.classification === "warm"
          ? body.classification
          : "cold";
      if (tiers[tier] == null || body.connect_ms < tiers[tier]) {
        tiers = { ...tiers, [tier]: body.connect_ms };
      }
      flashConnectPulse();
    } catch (err) {
      runError = String(err);
    } finally {
      running = false;
      stopStopwatch();
    }
  }

  function onTruncateClick() {
    if (running || truncating) return;
    if (!truncateConfirming) {
      truncateConfirming = true;
      return;
    }
    truncateConfirming = false;
    truncateLedger();
  }

  async function truncateLedger() {
    truncating = true;
    runError = "";
    try {
      const resp = await fetch(`${API}/truncate`, { method: "POST" });
      const body = await resp.json();
      if (!resp.ok) {
        runError = body?.detail || `truncate failed (${resp.status})`;
        return;
      }
      if (!body.truncated) {
        runError = body.error || "truncate failed";
        return;
      }
      lastRun = null;
      runs = [];
    } catch (err) {
      runError = String(err);
    } finally {
      truncating = false;
    }
  }

  async function resetInstance() {
    if (resetting) return;
    truncateConfirming = false;
    resetting = true;
    runError = "";
    try {
      const resp = await fetch(`${API}/reset`, { method: "POST" });
      if (!resp.ok) {
        const body = await resp.json().catch(() => null);
        runError = body?.detail || `reset failed (${resp.status})`;
      }
    } catch (err) {
      runError = String(err);
    } finally {
      resetting = false;
    }
  }

  onMount(() => {
    // Opening the tab IS the wake-on-connect demo: fire a read-only aggregate
    // unprompted so the visitor watches the VM wake for them without writing,
    // then start the lifecycle poll.
    runQuery("aggregate");
    pollStatus();
    pollTimer = setInterval(pollStatus, POLL_MS);
    return () => {
      clearInterval(pollTimer);
      stopStopwatch();
      if (pulseTimer) clearTimeout(pulseTimer);
    };
  });

  const WAKE_NARRATION = {
    banked: "connection parked, waking the VM",
    relighting: "relighting from snapshot",
    cold_booting: "cold booting against the volume",
    serving: "spliced through, running SQL",
  };

  let wakeNarration = $derived(
    WAKE_NARRATION[status?.state] ?? "connection parked, waking the VM",
  );

  const STATE_VIEW = {
    serving: { label: "Awake", tone: "awake", hint: "VM live, next query is warm" },
    banking: { label: "Falling asleep", tone: "drowsy", hint: "pausing to a snapshot bundle" },
    banked: { label: "Asleep", tone: "asleep", hint: "no VM running; a connection wakes it" },
    relighting: { label: "Waking (relight)", tone: "waking", hint: "resuming the paused snapshot" },
    cold_booting: { label: "Waking (cold boot)", tone: "waking", hint: "fresh boot against the volume" },
    starting: { label: "Starting", tone: "waking", hint: "first boot" },
  };

  let stateView = $derived(
    STATE_VIEW[status?.state] ?? {
      label: status?.state || "No instance yet",
      tone: "asleep",
      hint: "cold-boots on the first connection",
    },
  );

  const CLASS_LABEL = {
    warm: "warm (VM already awake)",
    relight: "relight (resumed from snapshot)",
    cold: "cold boot (fresh VM on the volume)",
    transitional: "caught mid-transition",
    unknown: "unclassified",
  };

  function ms(v) {
    if (v == null) return "–";
    return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
  }

  function clock(iso) {
    if (!iso) return "–";
    const d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleTimeString();
  }

  function money(v) {
    return `£${(v ?? 0).toFixed(2)}`;
  }

  function barPct(run, part) {
    const total = (run.connect_ms ?? 0) + (run.query_ms ?? 0);
    if (!total) return 0;
    return (part / total) * 100;
  }

  // Statements shown in the strip: everything the backend ran minus the
  // DDL-if-missing (real work, but not part of the story we're telling).
  let strippedStatements = $derived(
    (lastRun?.statements ?? []).filter((s) => !s.sql.startsWith("CREATE TABLE")),
  );

  // Group rows into epoch bands by postmaster_start, newest group first, so
  // the grid visually shows which rows share a resumed process and which
  // rows outlived a cold boot.
  let epochBands = $derived.by(() => {
    const rows = lastRun?.rows ?? [];
    const bands = [];
    for (const row of rows) {
      let band = bands[bands.length - 1];
      if (!band || band.postmaster_start !== row.postmaster_start) {
        band = { postmaster_start: row.postmaster_start, rows: [] };
        bands.push(band);
      }
      band.rows.push(row);
    }
    return bands;
  });

  let maxRevenue = $derived(
    Math.max(1, ...((lastRun?.breakdown ?? []).map((b) => b.revenue))),
  );

  // Asleep hero strip: the "0 vCPU" lead line replaces the muted hint when
  // the VM is fully banked, so the panel's rest state reads as a claim
  // rather than an absence.
  let asleepHero = $derived.by(() => {
    if (status?.state !== "banked") return null;
    const mib =
      status?.volume_bytes != null ? `${(status.volume_bytes / 1024 / 1024).toFixed(1)} MiB` : "some";
    const wake = tiers.relight != null ? ms(tiers.relight) : "under 100 ms";
    return `${mib} of orders on disk, waiting. The next connection brings Postgres back in ~${wake}.`;
  });

  // Dozing narration while serving-but-idle: an approach, not a countdown.
  let dozeHint = $derived(
    status?.state === "serving" && !running
      ? idleSeconds < 1
        ? "idle, falls asleep about a second after the last connection closes"
        : "dozing off any moment"
      : null,
  );
</script>

<section class="pg-panel">
  <div class="lifecycle-card">
    <div class="state-chip tone-{stateView.tone}">
      <span class="state-dot" aria-hidden="true"></span>
      <span class="state-label">{stateView.label}</span>
    </div>
    {#if asleepHero}
      <p class="state-hero">
        <strong>0 vCPU · 0 MiB RAM right now.</strong> {asleepHero}
      </p>
    {:else}
      <p class="state-hint">{dozeHint ?? stateView.hint}</p>
    {/if}
    <dl class="state-facts">
      <div>
        <dt>generation</dt>
        <dd>{status?.generation ?? "–"}</dd>
      </div>
      <div>
        <dt>snapshot pairs</dt>
        <dd>{status?.pair_valid == null ? "–" : status.pair_valid ? "yes" : "no"}</dd>
      </div>
      <div>
        <dt>volume</dt>
        <dd>
          {status?.volume_bytes != null
            ? `${(status.volume_bytes / 1024 / 1024).toFixed(1)} MiB`
            : "–"}
        </dd>
      </div>
    </dl>
    {#if statusError}
      <p class="soft-error">status: {statusError}</p>
    {/if}
  </div>

  <div class="controls">
    <button
      class="run-btn"
      type="button"
      onclick={() => runQuery("insert")}
      disabled={running}
      title="appends a random line item from the menu"
    >
      {running ? "Connecting…" : "INSERT an order"}
    </button>
    <button
      class="aggregate-btn"
      type="button"
      onclick={() => runQuery("aggregate")}
      disabled={running}
      title="SELECT only: wakes the VM without writing"
    >
      {running ? "Connecting…" : "Run aggregate"}
    </button>
    <div class="destructive-group">
      <button
        class="truncate-btn"
        class:confirming={truncateConfirming}
        type="button"
        onclick={onTruncateClick}
        disabled={running || truncating}
        title="TRUNCATE demo_orders. The VM lives, the data dies."
      >
        {truncating
          ? "Truncating…"
          : truncateConfirming
            ? "really truncate?"
            : "Clear ledger (TRUNCATE)"}
      </button>
      <button
        class="reset-btn"
        type="button"
        onclick={resetInstance}
        disabled={resetting || running}
        title="Destroy the VM and evict its snapshot. The data volume survives, so the next query pays a full cold boot against retained rows."
      >
        {resetting ? "Resetting…" : "Force cold boot"}
      </button>
    </div>
    <p class="destructive-caption">
      truncate keeps the VM and kills the data; cold boot keeps the data and
      kills the VM
    </p>
  </div>

  {#if runError}
    <p class="run-error">
      {runError}
      <span class="run-error-hint">
        (a refused connect usually means the wake-rate limiter; wait a beat and retry)
      </span>
    </p>
  {/if}

  {#if running}
    <div class="stopwatch-card">
      <span class="stopwatch-value">{ms(stopwatchMs)}</span>
      <span class="stopwatch-narration">{wakeNarration}</span>
    </div>
  {/if}

  {#if lastRun}
    <div class="timing-card">
      <div class="timing-headline">
        <div class="timing-big">
          <span class="timing-value" class:connect-pulse={connectPulse}>{ms(lastRun.connect_ms)}</span>
          <span class="timing-label">connect (the wake)</span>
        </div>
        <div class="timing-big">
          <span class="timing-value">{ms(lastRun.query_ms)}</span>
          <span class="timing-label">SQL roundtrip</span>
        </div>
        <div class="timing-class">{CLASS_LABEL[lastRun.classification] ?? lastRun.classification}</div>
      </div>
      <div class="timing-bar" aria-hidden="true">
        <span class="bar-connect" style:width="{barPct(lastRun, lastRun.connect_ms)}%"></span>
        <span class="bar-query" style:width="{barPct(lastRun, lastRun.query_ms)}%"></span>
      </div>
      <p class="timing-note">
        The connect bracket includes the whole wake: the activator parks your
        TCP connection while the microVM {lastRun.classification === "cold"
          ? "cold-boots"
          : "relights"}, then splices it through. Once awake, Postgres is just
        Postgres.
      </p>
    </div>

    <div class="tiers">
      <div class="tier">
        <span class="tier-value">{ms(tiers.cold)}</span>
        <span class="tier-label">cold boot</span>
      </div>
      <span class="tier-arrow" aria-hidden="true">›</span>
      <div class="tier">
        <span class="tier-value">{ms(tiers.relight)}</span>
        <span class="tier-label">relight</span>
      </div>
      <span class="tier-arrow" aria-hidden="true">›</span>
      <div class="tier">
        <span class="tier-value">{ms(tiers.warm)}</span>
        <span class="tier-label">warm</span>
      </div>
      <p class="tiers-caption">best connect time seen this session, per boot path</p>
    </div>
  {/if}

  {#if strippedStatements.length}
    <div class="statement-card">
      <h3 class="statement-title">statements run</h3>
      <ul class="statement-list">
        {#each strippedStatements as stmt, i (i)}
          <li class="statement-row">
            <code class="statement-sql">{stmt.sql}</code>
            <span class="statement-ms">-&gt; {ms(stmt.ms)}</span>
          </li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if lastRun?.breakdown?.length}
    <div class="aggregate-card">
      <p class="aggregate-headline">
        <span class="aggregate-revenue">{money(lastRun.total_revenue)} total revenue</span>
        · <span class="aggregate-orders">{lastRun.total_orders} orders</span>
      </p>
      <ul class="aggregate-list">
        {#each lastRun.breakdown as item (item.item)}
          <li class="aggregate-row">
            <span class="aggregate-item">{item.item}</span>
            <span class="aggregate-units">{item.units} units</span>
            <span class="aggregate-item-revenue">{money(item.revenue)}</span>
            <span class="aggregate-bar-track" aria-hidden="true">
              <span
                class="aggregate-bar-fill"
                style:width="{(item.revenue / maxRevenue) * 100}%"
              ></span>
            </span>
          </li>
        {/each}
      </ul>
    </div>
  {/if}

  {#if lastRun?.rows?.length}
    <div class="rows-card">
      <h3 class="rows-title">orders ledger</h3>
      <table class="rows-table">
        <thead>
          <tr>
            {#each COLUMN_TYPES as col (col.key)}
              <th class={col.key === "qty" || col.key === "unit_price" ? "col-numeric" : ""}>
                <span class="col-name">{col.label}</span>
                <span class="col-type">{col.type}</span>
              </th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each epochBands as band, i (band.postmaster_start)}
            <tr class="epoch-band" class:epoch-current={i === 0}>
              <td colspan={COLUMN_TYPES.length}>
                process born {clock(band.postmaster_start)}
                · {i === 0 ? "current" : "survived a later boot"}
              </td>
            </tr>
            {#each band.rows as row (row.id)}
              <tr class:row-new={row.id === lastRun?.inserted?.id}>
                <td class="col-numeric">{row.id}</td>
                <td>{row.item}</td>
                <td class="col-numeric">{row.qty}</td>
                <td class="col-numeric">{money(row.unit_price)}</td>
                <td>{clock(row.written_at)}</td>
              </tr>
            {/each}
          {/each}
        </tbody>
      </table>
      <p class="rows-footer">({lastRun.total_orders} rows)</p>
      <p class="rows-note">
        "Process born" is pg_postmaster_start_time(): rows sharing it were
        served by the same resumed process (a relight never restarts Postgres);
        a new value marks a cold boot, and older rows surviving it is the
        volume outliving the VM.
      </p>
    </div>
  {/if}

  {#if runs.length > 1}
    <div class="history">
      <h3 class="history-title">this session</h3>
      <ul class="history-list">
        {#each runs as run, i (runs.length - i)}
          <li class="history-row">
            <span class="history-class">{run.classification}</span>
            <span class="history-connect">{ms(run.connect_ms)} connect</span>
            <span class="history-query">{ms(run.query_ms)} query</span>
          </li>
        {/each}
      </ul>
    </div>
  {/if}
</section>

<style>
  .pg-panel {
    display: flex;
    flex-direction: column;
    gap: 18px;
    max-width: 860px;
  }

  .lifecycle-card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 18px 20px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 14px 22px;
  }

  .state-chip {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 8px 16px;
    border-radius: 999px;
    border: 1px solid var(--line);
    font-weight: 700;
    font-size: 15px;
  }

  .state-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--text-faint);
  }

  .tone-awake .state-dot {
    background: var(--svc-fc);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--svc-fc) 20%, transparent);
  }

  .tone-drowsy .state-dot,
  .tone-waking .state-dot {
    background: var(--loadtest-highlight);
    animation: pulse 0.9s ease-in-out infinite;
  }

  .tone-asleep .state-dot {
    background: var(--accent);
    opacity: 0.45;
  }

  .tone-asleep .state-label {
    color: var(--text-dim);
  }

  @keyframes pulse {
    0%,
    100% {
      transform: scale(1);
      opacity: 1;
    }
    50% {
      transform: scale(1.35);
      opacity: 0.55;
    }
  }

  .state-hint {
    margin: 0;
    color: var(--text-dim);
    font-size: 13px;
    flex: 1 1 200px;
  }

  .state-hero {
    margin: 0;
    color: var(--ink);
    font-size: 19px;
    line-height: 1.4;
    flex: 1 1 100%;
  }

  .state-hero strong {
    font-weight: 700;
  }

  .state-facts {
    display: flex;
    gap: 18px;
    margin: 0;
  }

  .state-facts dt {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-faint);
  }

  .state-facts dd {
    margin: 2px 0 0;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }

  .soft-error {
    flex-basis: 100%;
    margin: 0;
    color: var(--danger);
    font-size: 12px;
  }

  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  .run-btn,
  .aggregate-btn,
  .truncate-btn,
  .reset-btn {
    padding: 10px 18px;
    border-radius: 8px;
    border: 1px solid var(--line);
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
  }

  .run-btn {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--surface);
  }

  .aggregate-btn {
    background: var(--surface);
    color: var(--accent);
    border-color: var(--accent);
  }

  .run-btn:disabled,
  .aggregate-btn:disabled,
  .truncate-btn:disabled,
  .reset-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .destructive-group {
    display: flex;
    gap: 10px;
  }

  .truncate-btn,
  .reset-btn {
    background: var(--surface);
    color: var(--danger);
  }

  .truncate-btn.confirming {
    background: var(--danger);
    border-color: var(--danger);
    color: var(--surface);
  }

  .destructive-caption {
    flex-basis: 100%;
    margin: 0;
    font-size: 12px;
    color: var(--text-faint);
  }

  .run-error {
    margin: 0;
    color: var(--danger);
    font-size: 13px;
  }

  .run-error-hint {
    color: var(--text-dim);
  }

  .stopwatch-card {
    display: flex;
    align-items: baseline;
    gap: 14px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 14px 20px;
  }

  .stopwatch-value {
    font-size: 26px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--accent);
  }

  .stopwatch-narration {
    font-size: 13px;
    color: var(--text-dim);
  }

  .timing-card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 20px;
  }

  .timing-headline {
    display: flex;
    align-items: baseline;
    gap: 32px;
    flex-wrap: wrap;
  }

  .timing-big {
    display: flex;
    flex-direction: column;
  }

  .timing-value {
    font-size: 34px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }

  .timing-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-faint);
    margin-top: 4px;
  }

  .timing-class {
    margin-left: auto;
    font-size: 13px;
    font-weight: 600;
    color: var(--accent);
  }

  .timing-bar {
    display: flex;
    height: 10px;
    border-radius: 5px;
    overflow: hidden;
    margin-top: 16px;
    background: var(--paper);
  }

  .bar-connect {
    background: var(--accent);
  }

  .bar-query {
    background: var(--svc-fc);
  }

  .timing-note {
    margin: 12px 0 0;
    font-size: 13px;
    color: var(--text-dim);
  }

  .tiers {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    padding: 4px 6px;
  }

  .tier {
    display: flex;
    flex-direction: column;
  }

  .tier-value {
    font-size: 22px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .tier-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-faint);
  }

  .tier-arrow {
    color: var(--text-faint);
    font-size: 20px;
  }

  .tiers-caption {
    margin: 0 0 0 auto;
    font-size: 12px;
    color: var(--text-faint);
  }

  .statement-card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 16px 20px;
  }

  .statement-title {
    margin: 0 0 10px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
  }

  .statement-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .statement-row {
    display: flex;
    align-items: baseline;
    gap: 16px;
    font-size: 13px;
  }

  .statement-sql {
    flex: 1 1 auto;
    font-family: var(--font-mono, monospace);
    color: var(--ink);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .statement-ms {
    flex: 0 0 auto;
    font-family: var(--font-mono, monospace);
    text-align: right;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
  }

  .aggregate-card {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 20px;
  }

  .aggregate-headline {
    margin: 0 0 14px;
    font-size: 22px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .aggregate-revenue {
    color: var(--accent);
  }

  .aggregate-orders {
    color: var(--ink);
  }

  .aggregate-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .aggregate-row {
    display: grid;
    grid-template-columns: 1fr auto auto;
    align-items: center;
    column-gap: 14px;
    row-gap: 4px;
    font-size: 13px;
  }

  .aggregate-item {
    font-weight: 600;
  }

  .aggregate-units {
    color: var(--text-dim);
    font-variant-numeric: tabular-nums;
  }

  .aggregate-item-revenue {
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    text-align: right;
  }

  .aggregate-bar-track {
    grid-column: 1 / -1;
    height: 6px;
    border-radius: 3px;
    background: var(--paper);
    overflow: hidden;
  }

  .aggregate-bar-fill {
    display: block;
    height: 100%;
    background: var(--accent);
  }

  .rows-card,
  .history {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 18px 20px;
  }

  .rows-title,
  .history-title {
    margin: 0 0 12px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-dim);
  }

  .rows-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    font-family: var(--font-mono, monospace);
  }

  .rows-table th {
    text-align: left;
    padding: 4px 10px 8px 0;
    border-bottom: 1px solid var(--line);
    vertical-align: bottom;
  }

  .rows-table th.col-numeric {
    text-align: right;
  }

  .col-name {
    display: block;
    font-size: 12px;
    text-transform: lowercase;
    color: var(--ink);
    font-weight: 700;
  }

  .col-type {
    display: block;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-faint);
    font-weight: 400;
  }

  .rows-table td {
    padding: 6px 10px 6px 0;
    border-bottom: 1px solid var(--line);
    font-variant-numeric: tabular-nums;
  }

  .rows-table td.col-numeric {
    text-align: right;
  }

  .epoch-band td {
    padding: 6px 10px;
    font-family: var(--font-sans, inherit);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--text-dim);
    background: color-mix(in srgb, var(--accent) 8%, transparent);
    border-bottom: 1px solid var(--line);
  }

  .epoch-band.epoch-current td {
    background: color-mix(in srgb, var(--accent) 14%, transparent);
  }

  .rows-footer {
    margin: 10px 0 0;
    font-size: 12px;
    font-family: var(--font-mono, monospace);
    color: var(--text-faint);
  }

  .rows-note {
    margin: 12px 0 0;
    font-size: 12px;
    color: var(--text-dim);
  }

  .history-list {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .history-row {
    display: flex;
    gap: 18px;
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }

  .history-class {
    width: 90px;
    font-weight: 600;
    color: var(--accent);
  }

  .history-connect,
  .history-query {
    color: var(--text-dim);
  }

  @media (prefers-reduced-motion: no-preference) {
    .connect-pulse {
      display: inline-block;
      animation: connect-pulse 0.5s ease-out;
    }

    @keyframes connect-pulse {
      0% {
        transform: scale(1.25);
        color: var(--svc-fc);
      }
      100% {
        transform: scale(1);
        color: var(--ink);
      }
    }

    .row-new {
      animation: row-new-fade 0.8s ease-out;
    }

    @keyframes row-new-fade {
      0% {
        background: color-mix(in srgb, var(--accent) 35%, transparent);
      }
      100% {
        background: transparent;
      }
    }
  }
</style>
