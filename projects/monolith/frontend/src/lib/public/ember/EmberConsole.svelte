<script>
  // Demo-postgres: the embervm R4 stateful sleep/wake exhibit, public edition.
  // Adapted from the private panel (frontend/src/lib/private/components/demos/
  // PostgresPanel.svelte): same two-column console, stopwatch, savings ticker,
  // backoff, and you/visitor markers. Two differences from the private panel:
  //
  //   - No reset button. Force-cold-booting is a griefing vector on a public
  //     page (repeated 30-60s cold-boot purgatory for every other visitor), so
  //     the destructive control and its caption do not exist here at all.
  //   - Turnstile-gated inserts. When a site key is configured, a widget
  //     renders above the controls on first load; the solved token mints a
  //     session via the session proxy, and INSERT stays disabled with a short
  //     hint until that mint succeeds. Aggregate never needs a session. When no
  //     site key is configured (dev), this mints sessionlessly on mount exactly
  //     like the private panel, matching the backend's private-tier allowance.
  //
  // All three remaining calls are same-origin proxies under /ember/postgres/api
  // (see the +server.js routes beside this component's page), never direct
  // /api fetches: the public tier's rule 2 (public-tier-checklist.md).
  import { onMount } from "svelte";

  /** @type {{ turnstileSiteKey?: string, initialStatus?: object|null, initialSavings?: object|null }} */
  let { turnstileSiteKey = "", initialStatus = null, initialSavings = null } = $props();

  const API = "/ember/postgres/api";
  const POLL_MS = 700;

  // Resource-savings counter assumption: the demo VM is sized at 512 MiB, so
  // every second it spends banked is 512 MiB-seconds of RAM not spent. Not
  // measured, a stated assumption shown alongside the number.
  const MIB_SAVED_PER_S = 512;
  const SAVINGS_TICK_MS = 200;

  // Clicks during a lifecycle transition (mid-bank, wake-rate limiter) fail
  // transiently; retry the same request under the still-ticking stopwatch so
  // it reads as one longer wake, not repeated failures. Capped at 3s.
  const RETRY_DELAYS_MS = [500, 1000, 2000, 3000];

  const ORDER_COLUMNS = [
    { key: "id", label: "id", type: "bigserial" },
    { key: "item", label: "item", type: "text" },
    { key: "qty", label: "qty", type: "int" },
    { key: "unit_price", label: "unit_price", type: "numeric(8,2)" },
    { key: "written_at", label: "written_at", type: "timestamptz" },
    { key: "yours", label: "session", type: "who" },
  ];

  const ORDERS_SQL = "SELECT * FROM demo_orders ORDER BY id DESC";
  const SUMMARY_SQL =
    "SELECT item, SUM(qty * unit_price) FROM demo_orders GROUP BY item";

  // SSR-seeded initial paint: the page load already ran the same cached,
  // wake-safe status/savings reads server-side, so the hero stat and state
  // chip aren't blank before the client's own poll lands a moment later. A
  // savings-only seed (status missing) is merged onto the status shape so
  // total_saved_mib_s still renders while state waits for the first poll.
  let status = $state(
    initialStatus?.configured
      ? { ...initialStatus, total_saved_mib_s: initialSavings?.total_saved_mib_s ?? initialStatus.total_saved_mib_s }
      : initialSavings
        ? { total_saved_mib_s: initialSavings.total_saved_mib_s }
        : null,
  );
  let statusError = $state("");
  let running = $state(false);
  let lastRun = $state(null);
  let runError = $state("");

  // Which result shape the right column shows. INSERT lands on the orders
  // grid; the aggregate query switches to the summary table.
  let view = $state("orders");

  // Best-seen wall time per boot path: the demo's headline comparison.
  let tiers = $state({ cold: null, relight: null, warm: null });

  let pollTimer = null;

  // Live wake stopwatch: ticks via requestAnimationFrame while a query is in
  // flight, so the visitor watches the wake happen instead of just seeing the
  // final number appear.
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
    requestAnimationFrame(() => {
      connectPulse = true;
      if (pulseTimer) clearTimeout(pulseTimer);
      pulseTimer = setTimeout(() => {
        connectPulse = false;
      }, 500);
    });
  }

  // Falling-asleep narration: seconds since the VM's last activity, ticked by
  // the status poll (not a fabricated countdown, just a rough approach).
  let idleSeconds = $state(0);

  // True while a retry delay is in flight, so the narration line can say
  // "still waking, retrying" instead of the poll-driven wake narration or a
  // mid-sequence error.
  let retrying = $state(false);
  let retryTimeoutId = null;

  function sleep(delayMs) {
    return new Promise((resolve) => {
      retryTimeoutId = setTimeout(() => {
        retryTimeoutId = null;
        resolve();
      }, delayMs);
    });
  }

  function cancelPendingRetry() {
    if (retryTimeoutId != null) {
      clearTimeout(retryTimeoutId);
      retryTimeoutId = null;
    }
    retrying = false;
  }

  // Shared retry wrapper: runs doAttempt() up to 1 + RETRY_DELAYS_MS.length
  // times, waiting the next backoff delay between attempts. doAttempt should
  // return {ok: true, ...} on success or {ok: false, permanent, error} on a
  // failure worth surfacing; a thrown error is treated as transient.
  async function fetchWithBackoff(doAttempt) {
    let lastResult = null;
    for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
      try {
        const result = await doAttempt();
        if (result.ok || result.permanent) return result;
        lastResult = result;
      } catch (err) {
        lastResult = { ok: false, error: String(err) };
      }
      if (attempt < RETRY_DELAYS_MS.length) {
        retrying = true;
        await sleep(RETRY_DELAYS_MS[attempt]);
        retrying = false;
      }
    }
    return lastResult;
  }

  // Session-scoped RAM-savings counter: accumulates MiB-seconds while banked.
  // A coarse setInterval (not raf) is plenty for a number that only needs to
  // visibly tick, not animate smoothly.
  let savedMibSeconds = $state(0);
  let savingsTimer = null;
  let savingsLastTick = 0;

  function startSavingsTimer() {
    if (savingsTimer != null) return;
    savingsLastTick = performance.now();
    savingsTimer = setInterval(() => {
      const now = performance.now();
      const elapsedS = (now - savingsLastTick) / 1000;
      savingsLastTick = now;
      savedMibSeconds += elapsedS * MIB_SAVED_PER_S;
    }, SAVINGS_TICK_MS);
  }

  function stopSavingsTimer() {
    if (savingsTimer != null) {
      clearInterval(savingsTimer);
      savingsTimer = null;
    }
  }

  // Hourly reset countdown: the backend lazily deletes rows older than the
  // current hour on each query, so there's no event to listen for, just a
  // clock.
  let nowMs = $state(Date.now());
  let clockTimer = null;

  function startClockTimer() {
    clockTimer = setInterval(() => {
      nowMs = Date.now();
    }, 1000);
  }

  function stopClockTimer() {
    if (clockTimer != null) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  }

  let resetCountdown = $derived.by(() => {
    const now = new Date(nowMs);
    const next = new Date(now);
    next.setMinutes(0, 0, 0);
    next.setHours(next.getHours() + 1);
    const remainingS = Math.max(0, Math.round((next.getTime() - now.getTime()) / 1000));
    const mm = String(Math.floor(remainingS / 60)).padStart(2, "0");
    const ss = String(remainingS % 60).padStart(2, "0");
    const clockLabel = next.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return { mmss: `${mm}:${ss}`, clockLabel };
  });

  // Who-woke-it inference: client-side only, no backend signal for this.
  let prevState = null;
  let lastOwnActivityAt = 0;
  let othersWoke = $state(false);

  const WAKING_STATES = new Set(["relighting", "cold_booting", "starting", "serving"]);

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
      if (body.state === "banked") {
        startSavingsTimer();
        othersWoke = false;
      } else {
        stopSavingsTimer();
      }
      const wasAsleep = prevState == null || prevState === "banked";
      if (wasAsleep && WAKING_STATES.has(body.state) && !running) {
        othersWoke = true;
      }
      prevState = body.state;
    } catch (err) {
      statusError = String(err);
    }
  }

  // Turnstile gating: sessionReady becomes true once /session mints (or
  // confirms) a cookie. Sessionless-mint mode (no site key) starts true only
  // after the fire-and-forget mint on mount resolves ok, mirroring the
  // private panel's behavior closely enough that dev/no-key still works.
  let sessionReady = $state(false);
  let sessionError = $state("");

  async function mintSession(turnstileToken = "") {
    try {
      const resp = await fetch(`${API}/session`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ turnstile_token: turnstileToken }),
      });
      const body = await resp.json().catch(() => ({}));
      if (resp.ok && body.ok) {
        sessionReady = true;
        sessionError = "";
      } else if (!resp.ok) {
        sessionError = "verification failed, try again";
      }
    } catch {
      // fire-and-forget: a network hiccup just leaves inserts gated
    }
  }

  async function attemptQuery(mode) {
    const resp = await fetch(`${API}/query`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode }),
    });
    const body = await resp.json();
    if (resp.status === 503 && /not configured/i.test(body?.detail ?? "")) {
      // Permanent: no DSN configured on this deployment, retrying won't help.
      return { ok: false, permanent: true, error: body.detail };
    }
    if (!resp.ok) {
      return { ok: false, error: body?.detail || `query failed (${resp.status})` };
    }
    if (body.session_required) {
      return {
        ok: false,
        permanent: true,
        error: "solve the check above to add orders",
      };
    }
    if (body.rate_limited) {
      return { ok: false, permanent: true, error: body.error };
    }
    if (body.error) {
      return { ok: false, error: body.error };
    }
    return { ok: true, body };
  }

  async function runQuery(mode) {
    if (running) return;
    if (mode === "insert" && turnstileSiteKey && !sessionReady) return;
    running = true;
    runError = "";
    othersWoke = false;
    startStopwatch();
    try {
      const result = await fetchWithBackoff(() => attemptQuery(mode));
      if (!result.ok) {
        runError = result.error;
        return;
      }
      const body = result.body;
      lastRun = body;
      view = mode === "aggregate" ? "summary" : "orders";
      const tier =
        body.classification === "relight" || body.classification === "warm"
          ? body.classification
          : "cold";
      if (tiers[tier] == null || body.connect_ms < tiers[tier]) {
        tiers = { ...tiers, [tier]: body.connect_ms };
      }
      flashConnectPulse();
    } finally {
      running = false;
      lastOwnActivityAt = Date.now();
      cancelPendingRetry();
      stopStopwatch();
    }
  }

  onMount(() => {
    if (!turnstileSiteKey) {
      // No widget configured (dev): mint sessionlessly, matching the private
      // panel and the backend's private-tier allowance.
      mintSession("");
    }

    // Opening the tab IS the wake-on-connect demo: fire a read-only aggregate
    // unprompted so the visitor watches the VM wake for them without writing,
    // then start the lifecycle poll.
    runQuery("aggregate");
    pollStatus();
    pollTimer = setInterval(pollStatus, POLL_MS);
    startClockTimer();
    return () => {
      clearInterval(pollTimer);
      stopStopwatch();
      stopSavingsTimer();
      stopClockTimer();
      cancelPendingRetry();
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
    retrying
      ? "still waking, retrying"
      : (WAKE_NARRATION[status?.state] ?? "connection parked, waking the VM"),
  );

  const STATE_VIEW = {
    serving: {
      label: "Awake",
      tone: "awake",
      sentence:
        "Awake. Queries answer instantly; it falls back asleep about a second after the last one.",
    },
    banking: {
      label: "Falling asleep",
      tone: "drowsy",
      sentence: "Saving itself to disk...",
    },
    banked: { label: "Asleep", tone: "asleep", sentence: "" },
    relighting: {
      label: "Waking (from snapshot)",
      tone: "waking",
      sentence: "Waking up...",
    },
    cold_booting: {
      label: "Waking (cold start)",
      tone: "waking",
      sentence: "Waking up...",
    },
    starting: {
      label: "Starting",
      tone: "waking",
      sentence: "Waking up...",
    },
  };

  let stateView = $derived(
    STATE_VIEW[status?.state] ?? {
      label: status?.state || "No instance yet",
      tone: "asleep",
      sentence: "Cold-boots on the first connection.",
    },
  );

  // Classification of the last completed run, in plain words, so the
  // narration line under the wake number is always occupied.
  const CLASS_SENTENCE = {
    warm: "was already awake",
    relight: "woke from a snapshot",
    cold: "cold start: fresh boot against the saved data",
  };

  let lastRunSentence = $derived.by(() => {
    if (!lastRun) return "";
    if (lastRun.classification === "warm") return CLASS_SENTENCE.warm;
    if (lastRun.classification === "relight") {
      return `woke from a snapshot in ${ms(lastRun.connect_ms)}`;
    }
    if (lastRun.classification === "cold") return CLASS_SENTENCE.cold;
    return "";
  });

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

  function mibSeconds(v) {
    return v < 1024 ? `${Math.round(v)} MiB·s` : `${(v / 1024).toFixed(1)} GiB·s`;
  }

  // Compact number formatting shared by the aggregate headline and the
  // all-time savings line.
  function humanize(n) {
    const abs = Math.abs(n);
    if (abs < 1000) return `${Math.round(n)}`;
    const units = [
      { value: 1e9, suffix: "B" },
      { value: 1e6, suffix: "M" },
      { value: 1e3, suffix: "K" },
    ];
    for (const { value, suffix } of units) {
      if (abs >= value) {
        const scaled = n / value;
        const decimals = Math.abs(scaled) < 100 ? 1 : 0;
        return `${scaled.toFixed(decimals)}${suffix}`;
      }
    }
    return `${Math.round(n)}`;
  }

  function moneyHeadline(v) {
    const total = v ?? 0;
    return total >= 10000 ? `£${humanize(total)}` : money(total);
  }

  function countHeadline(v) {
    const total = v ?? 0;
    return total >= 10000 ? humanize(total) : `${total}`;
  }

  // All-time "memory saved while asleep" across every visitor, in GB-hours.
  function gbHours(mibSeconds) {
    if (mibSeconds == null) return "–";
    const gbh = mibSeconds / 1024 / 3600;
    if (gbh < 10) return `${gbh.toFixed(1)} GB·h`;
    return `${humanize(gbh)} GB·h`;
  }

  function barPct(run, part) {
    const total = (run.connect_ms ?? 0) + (run.query_ms ?? 0);
    if (!total) return 0;
    return (part / total) * 100;
  }

  // Group rows into epoch bands by postmaster_start, newest group first, so
  // the grid visually shows which rows share a resumed process and which rows
  // outlived a cold boot.
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

  // Asleep hero copy: the volume size and best-known relight time, so the
  // panel's rest state reads as a claim rather than an absence.
  let asleepMib = $derived(
    status?.volume_bytes != null
      ? `${(status.volume_bytes / 1024 / 1024).toFixed(1)} MiB`
      : "some",
  );

  let asleepWake = $derived(tiers.relight != null ? ms(tiers.relight) : "under 100 ms");

  // Dozing narration while serving-but-idle: an approach, not a countdown.
  let dozeHint = $derived(
    status?.state === "serving" && !running && idleSeconds >= 1
      ? "Dozing off any moment."
      : null,
  );

  // Who-woke-it narration.
  let othersHint = $derived(
    othersWoke
      ? "Another visitor just woke it."
      : status?.state === "serving" &&
          !running &&
          Date.now() - lastOwnActivityAt > 5000 &&
          idleSeconds < 1.5
        ? "Another visitor is using it right now."
        : null,
  );

  // Turnstile widget lifecycle: rendered lazily above the controls whenever a
  // site key is configured and no session exists yet. Mirrors
  // lib/public/components/TurnstileGate.svelte's script-load + render-once +
  // remove-on-unmount pattern rather than importing that component directly,
  // since this gate's admit callback needs to feed mintSession (an
  // ember-specific proxy path) rather than the chat admission module.
  const TURNSTILE_SCRIPT_SRC = "https://challenges.cloudflare.com/turnstile/v0/api.js";
  let widgetEl;
  let widgetId = null;

  function renderTurnstileWidget() {
    if (!window.turnstile || !widgetEl || !turnstileSiteKey || sessionReady) return;
    if (widgetId !== null) return;
    widgetId = window.turnstile.render(widgetEl, {
      sitekey: turnstileSiteKey,
      callback: (token) => mintSession(token),
      "error-callback": () => {
        sessionError = "verification failed, try again";
      },
    });
  }

  function removeTurnstileWidget() {
    if (widgetId !== null && window.turnstile) {
      try {
        window.turnstile.remove(widgetId);
      } catch {
        // widget already gone; nothing to clean up
      }
    }
    widgetId = null;
  }

  onMount(() => {
    if (!turnstileSiteKey) return undefined;
    if (window.turnstile) {
      renderTurnstileWidget();
      return removeTurnstileWidget;
    }
    let script = document.querySelector(`script[src="${TURNSTILE_SCRIPT_SRC}"]`);
    if (!script) {
      script = document.createElement("script");
      script.src = TURNSTILE_SCRIPT_SRC;
      script.async = true;
      script.defer = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", renderTurnstileWidget);
    return () => {
      script.removeEventListener("load", renderTurnstileWidget);
      removeTurnstileWidget();
    };
  });
</script>

<section class="pg-panel">
  <div class="left-col">
    <div class="state-block">
      <div class="state-chip tone-{stateView.tone}">
        <span class="state-dot" aria-hidden="true"></span>
        <span class="state-label">{stateView.label}</span>
      </div>
      {#if stateView.tone === "asleep" && status?.state === "banked"}
        <p class="state-sentence">
          <strong>Asleep, costing nothing.</strong> 0 CPU, 0 memory. Your {asleepMib}
          of data is safe on disk. The next query wakes the database in about
          {asleepWake}.
        </p>
      {:else}
        <p class="state-sentence">{dozeHint ?? othersHint ?? stateView.sentence}</p>
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
          <dd>{asleepMib === "some" ? "–" : asleepMib}</dd>
        </div>
      </dl>
      <p class="savings-line" class:savings-active={status?.state === "banked"}>
        memory saved while asleep this visit:
        <span class="savings-value">{mibSeconds(savedMibSeconds)}</span>
      </p>
      <p class="savings-caption">assuming the 512 MiB VM stayed running</p>
      <p class="alltime-savings">
        all visitors, all time: <span class="alltime-savings-value"
          >{gbHours(status?.total_saved_mib_s)}</span
        > saved while asleep
      </p>
      {#if statusError}
        <p class="soft-error">status: {statusError}</p>
      {/if}
    </div>

    {#if turnstileSiteKey && !sessionReady}
      <div class="turnstile-slot">
        <p class="turnstile-hint">solve the check to add orders</p>
        <div bind:this={widgetEl} class="turnstile-widget"></div>
        {#if sessionError}
          <p class="soft-error">{sessionError}</p>
        {/if}
      </div>
    {/if}

    <div class="controls">
      <button
        class="run-btn"
        type="button"
        onclick={() => runQuery("insert")}
        disabled={running || (!!turnstileSiteKey && !sessionReady)}
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
    </div>

    {#if runError}
      <p class="run-error">
        {runError}
        <span class="run-error-hint">
          (a refused connect usually means the wake-rate limiter; wait a beat and retry)
        </span>
      </p>
    {/if}

    <div class="last-run">
      <div class="last-run-numbers">
        <div class="last-run-big">
          <span class="last-run-value" class:connect-pulse={connectPulse}>
            {running ? ms(stopwatchMs) : ms(lastRun?.connect_ms)}
          </span>
          <span class="last-run-label">wake + connect</span>
        </div>
        <div class="last-run-big">
          <span class="last-run-value">{running ? "–" : ms(lastRun?.query_ms)}</span>
          <span class="last-run-label">query</span>
        </div>
      </div>
      <p class="last-run-narration">{running ? wakeNarration : lastRunSentence}</p>
      <div class="timing-bar" aria-hidden="true">
        {#if lastRun}
          <span class="bar-connect" style:width="{barPct(lastRun, lastRun.connect_ms)}%"></span>
          <span class="bar-query" style:width="{barPct(lastRun, lastRun.query_ms)}%"></span>
        {/if}
      </div>
    </div>

    <div class="tiers">
      <div class="tier">
        <span class="tier-value">{ms(tiers.cold)}</span>
        <span class="tier-label">cold start</span>
      </div>
      <span class="tier-arrow" aria-hidden="true">›</span>
      <div class="tier">
        <span class="tier-value">{ms(tiers.relight)}</span>
        <span class="tier-label">from snapshot</span>
      </div>
      <span class="tier-arrow" aria-hidden="true">›</span>
      <div class="tier">
        <span class="tier-value">{ms(tiers.warm)}</span>
        <span class="tier-label">already awake</span>
      </div>
      <p class="tiers-caption">best this session</p>
    </div>
  </div>

  <div class="right-col">
    <p class="formula-bar">{view === "summary" ? SUMMARY_SQL : ORDERS_SQL}</p>
    <p class="reset-countdown">
      ledger resets on the hour · <span class="reset-countdown-value"
        >{resetCountdown.mmss}</span
      >
      <span class="reset-countdown-clock">({resetCountdown.clockLabel})</span>
    </p>
    <div class="result-card">
      {#if view === "summary"}
        <div class="result-view swap-in">
          <table class="result-table">
            <thead>
              <tr>
                <th>
                  <span class="col-name">item</span>
                  <span class="col-type">text</span>
                </th>
                <th class="col-numeric">
                  <span class="col-name">units</span>
                  <span class="col-type">bigint</span>
                </th>
                <th class="col-numeric">
                  <span class="col-name">revenue</span>
                  <span class="col-type">numeric</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {#each lastRun?.breakdown ?? [] as item (item.item)}
                <tr class="summary-row">
                  <td>
                    <span
                      class="summary-bar"
                      style:width="{(item.revenue / maxRevenue) * 100}%"
                      aria-hidden="true"
                    ></span>
                    <span class="summary-item">{item.item}</span>
                  </td>
                  <td class="col-numeric">{item.units}</td>
                  <td class="col-numeric">{money(item.revenue)}</td>
                </tr>
              {/each}
            </tbody>
            <tfoot>
              <tr class="summary-total">
                <td>Σ total</td>
                <td class="col-numeric">{lastRun?.total_orders != null ? countHeadline((lastRun.breakdown ?? []).reduce((n, b) => n + b.units, 0)) : "–"} units</td>
                <td class="col-numeric">{moneyHeadline(lastRun?.total_revenue)}</td>
              </tr>
            </tfoot>
          </table>
          <p class="result-footer">
            ({(lastRun?.breakdown ?? []).length} groups from {lastRun?.total_orders ?? 0} orders)
          </p>
        </div>
      {:else}
        <div class="result-view swap-in">
          <table class="result-table">
            <thead>
              <tr>
                {#each ORDER_COLUMNS as col (col.key)}
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
                  <td colspan={ORDER_COLUMNS.length}>
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
                    <td class="col-session">
                      {#if row.yours}
                        <span class="yours-chip">you</span>
                      {:else}
                        <span class="visitor-label">visitor</span>
                      {/if}
                    </td>
                  </tr>
                {/each}
              {/each}
            </tbody>
          </table>
          <p class="result-footer">({lastRun?.total_orders ?? 0} rows)</p>
          <p class="result-note">
            Bands group rows written by the same database process. A new band
            means the VM was rebuilt from scratch; older rows surviving it
            show the data outlives the VM.
          </p>
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  .pg-panel {
    display: grid;
    grid-template-columns: 380px 1fr;
    align-items: start;
    gap: 20px;
    max-width: 1100px;
  }

  @media (max-width: 900px) {
    .pg-panel {
      grid-template-columns: 1fr;
    }

    .right-col {
      order: -1;
    }
  }

  .left-col {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .right-col {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }

  .state-block {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    padding: 18px 20px;
    min-height: 212px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .state-chip {
    display: inline-flex;
    align-items: center;
    gap: 10px;
    padding: 8px 16px;
    border-radius: 999px;
    border: 2px solid var(--ink);
    font-weight: 700;
    font-size: 15px;
    align-self: flex-start;
  }

  .state-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    background: var(--ink-3);
  }

  .tone-awake .state-dot {
    background: var(--teal);
    box-shadow: 0 0 0 4px color-mix(in srgb, var(--teal) 20%, transparent);
  }

  .tone-drowsy .state-dot,
  .tone-waking .state-dot {
    background: var(--accent);
    animation: pulse 0.9s ease-in-out infinite;
  }

  .tone-asleep .state-dot {
    background: var(--blue);
    opacity: 0.45;
  }

  .tone-asleep .state-label {
    color: var(--ink-3);
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

  .state-sentence {
    margin: 0;
    color: var(--ink);
    font-size: 15px;
    line-height: 1.45;
    min-height: 2.9em;
  }

  .state-sentence strong {
    font-weight: 700;
  }

  .state-facts {
    display: flex;
    gap: 18px;
    margin: 0;
    opacity: 0.7;
  }

  .state-facts dt {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
  }

  .state-facts dd {
    margin: 2px 0 0;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
  }

  .savings-line {
    margin: 0;
    font-size: 13px;
    color: var(--ink-3);
    opacity: 0.6;
  }

  .savings-line.savings-active {
    color: var(--ink);
    opacity: 1;
  }

  .savings-value {
    display: inline-block;
    min-width: 8ch;
    font-variant-numeric: tabular-nums;
    font-weight: 700;
  }

  .savings-caption {
    margin: -8px 0 0;
    font-size: 11px;
    color: var(--ink-3);
  }

  .alltime-savings {
    margin: 0;
    font-size: 11px;
    color: var(--ink-3);
  }

  .alltime-savings-value {
    display: inline-block;
    min-width: 6ch;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--ink-2);
  }

  .soft-error {
    margin: 0;
    color: var(--coral);
    font-size: 12px;
  }

  .turnstile-slot {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    padding: 14px 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .turnstile-hint {
    margin: 0;
    font-size: 12px;
    color: var(--ink-3);
  }

  .controls {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .run-btn,
  .aggregate-btn {
    padding: 10px 18px;
    border-radius: var(--radius);
    border: 2px solid var(--ink);
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    min-width: 100%;
    box-sizing: border-box;
  }

  .run-btn {
    background: var(--accent);
    color: var(--ink);
  }

  .aggregate-btn {
    background: var(--paper);
    color: var(--ink);
  }

  .run-btn:disabled,
  .aggregate-btn:disabled {
    opacity: 0.6;
    cursor: default;
  }

  .run-error {
    margin: 0;
    color: var(--coral);
    font-size: 13px;
  }

  .run-error-hint {
    color: var(--ink-2);
  }

  .last-run {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    padding: 18px 20px;
  }

  .last-run-numbers {
    display: flex;
    gap: 28px;
  }

  .last-run-big {
    display: flex;
    flex-direction: column;
  }

  .last-run-value {
    font-size: 30px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    line-height: 1.1;
  }

  .last-run-label {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    margin-top: 4px;
  }

  .last-run-narration {
    margin: 10px 0 0;
    font-size: 13px;
    color: var(--ink-2);
    min-height: 1.4em;
  }

  .timing-bar {
    display: flex;
    height: 8px;
    border-radius: 4px;
    overflow: hidden;
    margin-top: 12px;
    background: var(--bg-elev);
  }

  .bar-connect {
    background: var(--accent);
  }

  .bar-query {
    background: var(--teal);
  }

  .tiers {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    padding: 14px 18px;
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
  }

  .tier {
    display: flex;
    flex-direction: column;
  }

  .tier-value {
    font-size: 20px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .tier-label {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
  }

  .tier-arrow {
    color: var(--ink-3);
    font-size: 18px;
  }

  .tiers-caption {
    margin: 0 0 0 auto;
    font-size: 12px;
    color: var(--ink-3);
  }

  .formula-bar {
    margin: 0;
    padding: 6px 4px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
  }

  .reset-countdown {
    margin: 0;
    padding: 0 4px;
    font-size: 12px;
    color: var(--ink-3);
  }

  .reset-countdown-value {
    display: inline-block;
    min-width: 5ch;
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--ink-2);
  }

  .reset-countdown-clock {
    color: var(--ink-3);
  }

  .result-card {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    padding: 18px 20px;
    min-height: 420px;
  }

  .result-view {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  @media (prefers-reduced-motion: no-preference) {
    .result-view.swap-in {
      animation: result-crossfade 150ms ease-out;
    }

    @keyframes result-crossfade {
      0% {
        opacity: 0;
      }
      100% {
        opacity: 1;
      }
    }
  }

  .result-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    font-family: var(--mono);
  }

  .result-table th {
    text-align: left;
    padding: 4px 10px 8px 0;
    border-bottom: 1px solid var(--rule);
    vertical-align: bottom;
  }

  .result-table th.col-numeric {
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
    color: var(--ink-3);
    font-weight: 400;
  }

  .result-table td {
    padding: 6px 10px 6px 0;
    border-bottom: 1px solid var(--rule);
    font-variant-numeric: tabular-nums;
  }

  .result-table td.col-numeric {
    text-align: right;
  }

  .col-session {
    white-space: nowrap;
  }

  .yours-chip {
    display: inline-block;
    padding: 1px 8px;
    border-radius: 999px;
    background: color-mix(in srgb, var(--accent) 40%, transparent);
    color: var(--ink);
    font-size: 11px;
    font-weight: 700;
    text-transform: lowercase;
  }

  .visitor-label {
    font-size: 11px;
    color: var(--ink-3);
    text-transform: lowercase;
  }

  .epoch-band td {
    padding: 6px 10px;
    font-family: var(--sans);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--ink-2);
    background: var(--bg-elev);
    border-bottom: 1px solid var(--rule);
  }

  .epoch-band.epoch-current td {
    background: color-mix(in srgb, var(--accent) 25%, var(--bg-elev));
  }

  .summary-row td:first-child {
    position: relative;
  }

  .summary-bar {
    position: absolute;
    inset: 2px 0;
    background: color-mix(in srgb, var(--accent) 30%, transparent);
    border-radius: 3px;
    z-index: 0;
  }

  .summary-item {
    position: relative;
    z-index: 1;
    padding-left: 6px;
    font-weight: 600;
  }

  .summary-total td {
    border-bottom: none;
    border-top: 2px solid var(--rule-2);
    font-weight: 700;
    padding-top: 10px;
  }

  .result-footer {
    margin: 10px 0 0;
    font-size: 12px;
    font-family: var(--mono);
    color: var(--ink-3);
  }

  .result-note {
    margin: 12px 0 0;
    font-size: 12px;
    color: var(--ink-2);
  }

  @media (prefers-reduced-motion: no-preference) {
    .connect-pulse {
      display: inline-block;
      animation: connect-pulse 0.5s ease-out;
    }

    @keyframes connect-pulse {
      0% {
        transform: scale(1.25);
        color: var(--teal);
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
        background: color-mix(in srgb, var(--accent) 45%, transparent);
      }
      100% {
        background: transparent;
      }
    }
  }
</style>
