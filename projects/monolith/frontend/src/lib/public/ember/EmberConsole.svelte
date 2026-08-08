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
  import { fade } from "svelte/transition";
  import {
    classifyTier,
    includedSnapshotWait,
    parkedMsBreakdown,
    phaseLabel,
    shouldRetry,
  } from "./console-retry.js";

  /** @type {{ turnstileSiteKey?: string, initialStatus?: object|null, initialSavings?: object|null, status?: object|null, running?: boolean, stopwatchMs?: number }} */
  let {
    turnstileSiteKey = "",
    initialStatus = null,
    initialSavings = null,
    status = $bindable(null),
    running = $bindable(false),
    stopwatchMs = $bindable(0),
    wakePromise = $bindable(""),
  } = $props();

  const API = "/ember/postgres/api";
  const POLL_MS = 700;

  // Clicks during a lifecycle transition (mid-bank, wake-rate limiter) fail
  // transiently; retry the same request under the still-ticking stopwatch so
  // it reads as one longer wake, not repeated failures. Capped at 3s.
  const RETRY_DELAYS_MS = [500, 1000, 2000, 3000];
  // Transient failures are sub-second (in-band busy, mid-transition refusals),
  // so all retries happen within 6.5s of delays. A slow failure stops after
  // one attempt, capping the worst case at about 90s instead of 375+s. A
  // legitimate cold boot can take up to 60s, so in-flight attempts continue.
  const RETRY_WINDOW_MS = 30_000;

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
  status = initialStatus?.configured
    ? {
        ...initialStatus,
        total_saved_mib_s:
          initialSavings?.total_saved_mib_s ?? initialStatus.total_saved_mib_s,
      }
    : initialSavings
      ? { total_saved_mib_s: initialSavings.total_saved_mib_s }
      : null;
  let statusError = $state("");
  let lastRun = $state(null);
  let runError = $state("");

  // Which result shape the right column shows. INSERT lands on the orders
  // grid; the aggregate query switches to the summary table.
  let view = $state("orders");

  // Best-seen wall time per boot path: the demo's headline comparison.
  let tiers = $state({ cold: null, relight: null, warm: null });

  // Every run's wake+connect this session, newest last, capped for the
  // sparkline. Log-scaled bars: values legitimately span ~6ms to ~30s.
  const SPARK_MAX = 24;
  let runHistory = $state([]);

  // Session-relative log scale: bars normalize to THIS session's range, so
  // an all-warm session reads as a full band of consistently-fast runs
  // instead of dots under empty air, while a mixed session keeps the tall
  // cold bar -> short warm bar drama.
  function sparkHeight(connectMs) {
    const vals = runHistory.map((r) => r.connect_ms);
    if (vals.length === 0) return 0;
    const lo = Math.max(1, Math.min(...vals) * 0.8);
    const hi = Math.max(...vals) * 1.1;
    if (hi / lo < 1.05) return 60;
    const frac =
      (Math.log10(connectMs) - Math.log10(lo)) /
      (Math.log10(hi) - Math.log10(lo));
    return Math.max(12, Math.min(100, Math.round(frac * 100)));
  }

  let pollTimer = null;

  // Live wake stopwatch: ticks via requestAnimationFrame while a query is in
  // flight, so the visitor watches the wake happen instead of just seeing the
  // final number appear. stopwatchMs/running/status are bindable props (see
  // $props above) so the page can lift them into EmberStage without a second
  // poll loop.
  let stopwatchRaf = null;
  let stopwatchStart = 0;

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

  let retryTimeoutId = null;

  // Client-side rate limit shared by both buttons: at most one request per
  // second, enforced as a cooldown after every run settles. Stops repeat
  // mashing from racing the backend's own limiter and the VM's bank cycle.
  const COOLDOWN_MS = 1000;
  let cooldown = $state(false);
  let cooldownTimer = null;

  // Clicks during a run or the pacing gap queue up (capped) instead of
  // being dropped by a greyed-out button: labels stay put (no flashing),
  // a small chip shows the queue depth, and the queue drains itself at the
  // one-per-second pace.
  const QUEUE_CAP = 3;
  let queued = $state([]);

  let queuedInserts = $derived(queued.filter((m) => m === "insert").length);
  let queuedAggregates = $derived(
    queued.filter((m) => m === "aggregate").length,
  );

  function requestRun(mode) {
    if (
      mode === "insert" &&
      ((!!turnstileSiteKey && !sessionReady) || insertUnavailable)
    ) {
      return;
    }
    if (running || cooldown) {
      if (queued.length < QUEUE_CAP) queued = [...queued, mode];
      return;
    }
    runQuery(mode);
  }

  function startCooldown() {
    cooldown = true;
    if (cooldownTimer) clearTimeout(cooldownTimer);
    cooldownTimer = setTimeout(() => {
      cooldown = false;
      cooldownTimer = null;
      const next = queued[0];
      if (next) {
        queued = queued.slice(1);
        runQuery(next);
      }
    }, COOLDOWN_MS);
  }

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
  }

  // Shared retry wrapper: runs doAttempt() up to 1 + RETRY_DELAYS_MS.length
  // times, waiting the next backoff delay between attempts. doAttempt should
  // return {ok: true, ...} on success or {ok: false, permanent, error} on a
  // failure worth surfacing; a thrown error is treated as transient.
  async function fetchWithBackoff(doAttempt) {
    const startTime = performance.now();
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
        if (!shouldRetry(performance.now() - startTime, RETRY_WINDOW_MS)) {
          return {
            ...lastResult,
            error:
              "the demo took too long to answer and may be waking from cold, try again in a moment",
          };
        }
        await sleep(RETRY_DELAYS_MS[attempt]);
      }
    }
    return lastResult;
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
    const remainingS = Math.max(
      0,
      Math.round((next.getTime() - now.getTime()) / 1000),
    );
    const mm = String(Math.floor(remainingS / 60)).padStart(2, "0");
    const ss = String(remainingS % 60).padStart(2, "0");
    const clockLabel = next.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
    });
    return { mmss: `${mm}:${ss}`, clockLabel };
  });

  // Timestamp of the visitor's own last completed run; the stale-poll guard
  // below keys off it.
  let lastOwnActivityAt = 0;

  // Ephemeral per-page client id for the live-watchers count. Minted once in
  // onMount (never at SSR/module eval, hence the empty default), carried on
  // every status poll as ?p=. Opaque and not persisted: it identifies this
  // open tab for the ~6s presence TTL, nothing more, and is unrelated to the
  // insert session cookie.
  let clientId = "";

  // Parse a response body as JSON, returning null instead of throwing when the
  // body is not JSON. The public gateway enforces a coarse rate limit and
  // answers 429 with a PLAIN-TEXT body ("local_rate_limited"), and other edge
  // errors can be non-JSON too; calling resp.json() on those blows up with a
  // "Unexpected token" SyntaxError that used to leak straight into the panel.
  async function parseJsonSafe(resp) {
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  async function pollStatus() {
    try {
      const url = clientId
        ? `${API}/status?p=${encodeURIComponent(clientId)}`
        : `${API}/status`;
      const resp = await fetch(url);
      // A rate-limited or non-JSON poll is not worth alarming about: keep the
      // last good status and let the next poll (or a click) recover. Never
      // surface a raw parse error for a background poll.
      if (resp.status === 429) return;
      const body = await parseJsonSafe(resp);
      if (body == null) return;
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
      // Stale-poll guard: the status read is cached ~500ms and polled at
      // 700ms, so right after a completed run the poll can still say
      // "banked" while we already know the VM served us. Accepting that
      // regression makes the stage sweep backwards then forwards again;
      // hold "serving" until the poll data is plausibly fresher than our
      // own roundtrip.
      const staleCold =
        status?.state === "serving" &&
        (body.state === "banked" ||
          body.state === "checkpointed" ||
          body.state === "banking") &&
        Date.now() - lastOwnActivityAt < 2500;
      status = staleCold ? { ...body, state: "serving" } : body;
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
  // True when there is no widget to solve AND the sessionless mint was
  // refused (backend demands Turnstile, e.g. a local preview without a site
  // key): inserts are disabled outright rather than erroring toward a check
  // that does not exist.
  let insertUnavailable = $state(false);

  async function mintSession(turnstileToken = "") {
    try {
      const resp = await fetch(`${API}/session`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ turnstile_token: turnstileToken }),
      });
      // A 429 here is the gateway rate limit, not a verification failure: leave
      // inserts gated as-is and let a later mint retry, rather than latching
      // insertUnavailable off a transient limit.
      if (resp.status === 429) return;
      const body = (await parseJsonSafe(resp)) ?? {};
      if (resp.ok && body.ok) {
        sessionReady = true;
        sessionError = "";
        insertUnavailable = false;
      } else if (!resp.ok) {
        if (turnstileSiteKey) {
          sessionError = "verification failed, try again";
        } else {
          insertUnavailable = true;
        }
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
    // The public gateway answers a tripped rate limit with 429 and a plain-text
    // "local_rate_limited" body. Handle it before any JSON parse, and mark it
    // permanent for this run so fetchWithBackoff does NOT retry, hammering a
    // limiter that is already saying slow down only deepens the wait.
    if (resp.status === 429) {
      return {
        ok: false,
        permanent: true,
        error:
          "the demo is busy right now, give it a few seconds and try again",
      };
    }
    const body = await parseJsonSafe(resp);
    if (body == null) {
      // Non-JSON on any other status (a gateway error page, a proxy hiccup):
      // surface it calmly instead of leaking a raw JSON SyntaxError.
      return {
        ok: false,
        error: `unexpected response from the demo (${resp.status})`,
      };
    }
    if (resp.status === 503 && /not configured/i.test(body?.detail ?? "")) {
      // Permanent: no DSN configured on this deployment, retrying won't help.
      return { ok: false, permanent: true, error: body.detail };
    }
    if (!resp.ok) {
      return {
        ok: false,
        error: body?.detail || `query failed (${resp.status})`,
      };
    }
    if (body.session_required) {
      if (!turnstileSiteKey) insertUnavailable = true;
      return {
        ok: false,
        permanent: true,
        error: turnstileSiteKey
          ? "solve the check above to add orders"
          : "inserts need the human check, which is not configured here",
      };
    }
    if (body.rate_limited) {
      return {
        ok: false,
        permanent: true,
        error: "rate limited to one order per second, wait a moment and retry",
      };
    }
    if (body.error) {
      return { ok: false, error: body.error };
    }
    return { ok: true, body };
  }

  async function runQuery(mode) {
    if (running) return;
    running = true;
    runError = "";
    startStopwatch();
    try {
      const result = await fetchWithBackoff(() => attemptQuery(mode));
      if (!result.ok) {
        runError = result.error;
        return;
      }
      const body = result.body;
      lastRun = body;
      resultPage = 0;
      // A completed roundtrip MEANS the VM is serving right now; reflect it
      // immediately instead of letting the stage fall back to "banked" for
      // the up-to-1.2s gap (500ms status cache + 700ms poll) between running
      // flipping false and the poll confirming. Without this the warm sweep
      // visibly dips mid-wake; the next poll still owns the true state.
      if (status?.state !== "serving") {
        status = { ...(status ?? {}), state: "serving" };
      }
      view = mode === "aggregate" ? "summary" : "orders";
      const tier = classifyTier(body.classification);
      if (
        tier !== null &&
        (tiers[tier] == null || body.connect_ms < tiers[tier])
      ) {
        tiers = { ...tiers, [tier]: body.connect_ms };
      }
      runHistory = [
        ...runHistory.slice(-(SPARK_MAX - 1)),
        { connect_ms: body.connect_ms, tier: tier ?? "unclear" },
      ];
    } finally {
      running = false;
      lastOwnActivityAt = Date.now();
      cancelPendingRetry();
      stopStopwatch();
      startCooldown();
    }
  }

  onMount(() => {
    // Mint the ephemeral watcher id before the first poll so this tab is
    // counted from its opening request. crypto.randomUUID is available on the
    // https public site and on localhost; the Math.random fallback covers any
    // context without it (the id only needs to be unique-ish per tab).
    clientId =
      globalThis.crypto?.randomUUID?.() ??
      `c-${Math.random().toString(36).slice(2)}${Math.random().toString(36).slice(2)}`;

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
      stopClockTimer();
      cancelPendingRetry();
      if (cooldownTimer) clearTimeout(cooldownTimer);
    };
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

  // Compact number formatting for the aggregate headline.
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

  // Flattened band-header + row items, newest first, paged to a fixed count
  // so the result card never grows past its locked height: the ledger
  // paginates instead of pushing the page down.
  const PAGE_ITEMS = 12;
  let resultPage = $state(0);

  let orderItems = $derived.by(() => {
    const rows = lastRun?.rows ?? [];
    const items = [];
    let lastStart = null;
    let bandIndex = -1;
    for (const row of rows) {
      if (lastStart !== row.postmaster_start) {
        lastStart = row.postmaster_start;
        bandIndex += 1;
        items.push({
          type: "band",
          key: `band-${row.postmaster_start}`,
          postmaster_start: row.postmaster_start,
          current: bandIndex === 0,
        });
      }
      items.push({ type: "row", key: `row-${row.id}`, row });
    }
    return items;
  });

  let orderPageCount = $derived(
    Math.max(1, Math.ceil(orderItems.length / PAGE_ITEMS)),
  );

  let pagedOrderItems = $derived.by(() => {
    const page = Math.min(resultPage, orderPageCount - 1);
    return orderItems.slice(page * PAGE_ITEMS, (page + 1) * PAGE_ITEMS);
  });

  let shownPage = $derived(Math.min(resultPage, orderPageCount - 1));

  let maxRevenue = $derived(
    Math.max(1, ...(lastRun?.breakdown ?? []).map((b) => b.revenue)),
  );

  // The wake promise: best measured relight this session, else the honest
  // ceiling. Published as a bindable so the stage's asleep overlay shows it
  // (the separate state card duplicated the stage and is gone).
  let wakeHeadline = $derived(
    tiers.relight != null ? `~${ms(tiers.relight)}` : "<1 s",
  );

  $effect(() => {
    wakePromise = wakeHeadline;
  });

  // Turnstile widget lifecycle: rendered lazily above the controls whenever a
  // site key is configured and no session exists yet. Mirrors
  // lib/public/components/TurnstileGate.svelte's script-load + render-once +
  // remove-on-unmount pattern rather than importing that component directly,
  // since this gate's admit callback needs to feed mintSession (an
  // ember-specific proxy path) rather than the chat admission module.
  const TURNSTILE_SCRIPT_SRC =
    "https://challenges.cloudflare.com/turnstile/v0/api.js";
  let widgetEl;
  let widgetId = null;

  function renderTurnstileWidget() {
    if (!window.turnstile || !widgetEl || !turnstileSiteKey || sessionReady)
      return;
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
    let script = document.querySelector(
      `script[src="${TURNSTILE_SCRIPT_SRC}"]`,
    );
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
        onclick={() => requestRun("insert")}
        disabled={(!!turnstileSiteKey && !sessionReady) || insertUnavailable}
      >
        {insertUnavailable ? "INSERTs unavailable" : "INSERT an order"}
        {#if queuedInserts > 0}<span class="queue-chip"
            >+{queuedInserts} queued</span
          >{/if}
      </button>
      <button
        class="aggregate-btn"
        type="button"
        onclick={() => requestRun("aggregate")}
      >
        Run aggregate
        {#if queuedAggregates > 0}<span class="queue-chip"
            >+{queuedAggregates} queued</span
          >{/if}
      </button>
    </div>

    {#if runError}
      <p class="run-error">{runError}</p>
    {/if}
    {#if statusError}
      <p class="soft-error">status: {statusError}</p>
    {/if}

    <div class="stats-card">
      <div class="stats-section">
        <span class="stats-label">Last run</span>
        <div class="stat-row">
          <span class="stat-name">wake + connect</span>
          <span class="stat-value" class:stat-live={running}>
            {#key running ? "live" : (lastRun?.connect_ms ?? "none")}
              <span class="fade-swap" in:fade={{ duration: 220 }}>
                {running ? ms(stopwatchMs) : ms(lastRun?.connect_ms)}
              </span>
              {#if running && phaseLabel(status?.state)}
                <span class="stat-phase">{phaseLabel(status?.state)}</span>
              {/if}
            {/key}
          </span>
        </div>
        <!-- The completed-run attribution gets its OWN line rather than sitting
             beside the value: the left column is 340px and .stat-row is a
             space-between flex, so a sentence next to the number wraps and
             pushes the whole stats card taller. The live phaseLabel above is
             two words and does fit inline. -->
        <!-- {#if ... as x} is NOT Svelte: `as` bindings exist only on {#each}
             and {#await}. Bind the breakdown with {@const} inside the block
             instead, and fall back to the bare attribution when the backend
             could not attribute a park to this request (see the correlation
             note in ember_public/router.py: no attribution is far better than
             a fabricated split). -->
        {#if !running && lastRun}
          {@const breakdown = parkedMsBreakdown(lastRun)}
          {#if breakdown}
            <p class="stat-note">
              {ms(breakdown.total)} total: {ms(breakdown.parked)} waiting for the
              snapshot, {ms(breakdown.wake)} actually waking
            </p>
          {:else if includedSnapshotWait(lastRun)}
            <p class="stat-note">
              this run waited for the snapshot to finish writing
            </p>
          {/if}
        {/if}
        <div class="stat-row">
          <span class="stat-name">query</span>
          <span class="stat-value">
            {#key running ? "live" : (lastRun?.query_ms ?? "none")}
              <span class="fade-swap" in:fade={{ duration: 220 }}>
                {running ? "–" : ms(lastRun?.query_ms)}
              </span>
            {/key}
          </span>
        </div>
        <div class="stat-row">
          <span class="stat-name">total</span>
          <span class="stat-value">
            {#key running || !lastRun ? "none" : lastRun.connect_ms}
              <span class="fade-swap" in:fade={{ duration: 220 }}>
                {running || !lastRun
                  ? "–"
                  : ms((lastRun.connect_ms ?? 0) + (lastRun.query_ms ?? 0))}
              </span>
            {/key}
          </span>
        </div>

        <div class="spark-wrap">
          <div class="spark" aria-hidden="true">
            {#each runHistory as run, i (i)}
              <span
                class:spark-unclear={run.tier === "unclear"}
                class:spark-cold={run.tier === "cold"}
                class:spark-warm={run.tier === "warm"}
                class:spark-relight={run.tier === "relight"}
                class="spark-bar"
                style:height="{sparkHeight(run.connect_ms)}%"
              ></span>
            {/each}
          </div>
          <span class="spark-caption"
            >wake + connect, every run this session</span
          >
        </div>
      </div>

      <div class="stats-section stats-best">
        <span class="stats-label">Best times this session</span>
        <div class="stat-row">
          <span class="stat-name">cold start</span>
          <span class="stat-value">
            {#key tiers.cold}<span class="fade-swap" in:fade={{ duration: 220 }}
                >{ms(tiers.cold)}</span
              >{/key}
          </span>
        </div>
        <div class="stat-row">
          <span class="stat-name">from snapshot</span>
          <span class="stat-value">
            {#key tiers.relight}<span
                class="fade-swap"
                in:fade={{ duration: 220 }}>{ms(tiers.relight)}</span
              >{/key}
          </span>
        </div>
        <div class="stat-row">
          <span class="stat-name">already awake</span>
          <span class="stat-value">
            {#key tiers.warm}<span class="fade-swap" in:fade={{ duration: 220 }}
                >{ms(tiers.warm)}</span
              >{/key}
          </span>
        </div>
      </div>
    </div>
  </div>

  <div class="right-col">
    <div class="console-header">
      <p class="formula-bar">{view === "summary" ? SUMMARY_SQL : ORDERS_SQL}</p>
      <p class="reset-countdown">
        resets on the hour · <span class="reset-countdown-value"
          >{resetCountdown.mmss}</span
        >
      </p>
    </div>
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
                <td class="col-numeric"
                  >{lastRun?.total_orders != null
                    ? countHeadline(
                        (lastRun.breakdown ?? []).reduce(
                          (n, b) => n + b.units,
                          0,
                        ),
                      )
                    : "–"} units</td
                >
                <td class="col-numeric"
                  >{moneyHeadline(lastRun?.total_revenue)}</td
                >
              </tr>
            </tfoot>
          </table>
          <p class="result-footer">
            ({(lastRun?.breakdown ?? []).length} groups from {lastRun?.total_orders ??
              0} orders)
          </p>
        </div>
      {:else}
        <div class="result-view swap-in">
          <table class="result-table">
            <thead>
              <tr>
                {#each ORDER_COLUMNS as col (col.key)}
                  <th
                    class={col.key === "qty" || col.key === "unit_price"
                      ? "col-numeric"
                      : ""}
                  >
                    <span class="col-name">{col.label}</span>
                    <span class="col-type">{col.type}</span>
                  </th>
                {/each}
              </tr>
            </thead>
            <tbody>
              {#each pagedOrderItems as item (item.key)}
                {#if item.type === "band"}
                  <tr class="epoch-band" class:epoch-current={item.current}>
                    <td colspan={ORDER_COLUMNS.length}>
                      process born {clock(item.postmaster_start)}
                      · {item.current ? "current" : "survived a later boot"}
                    </td>
                  </tr>
                {:else}
                  <tr class:row-new={item.row.id === lastRun?.inserted?.id}>
                    <td class="col-numeric">{item.row.id}</td>
                    <td>{item.row.item}</td>
                    <td class="col-numeric">{item.row.qty}</td>
                    <td class="col-numeric">{money(item.row.unit_price)}</td>
                    <td>{clock(item.row.written_at)}</td>
                    <td class="col-session">
                      {#if item.row.yours}
                        <span class="yours-chip">you</span>
                      {:else}
                        <span class="visitor-label">visitor</span>
                      {/if}
                    </td>
                  </tr>
                {/if}
              {/each}
            </tbody>
          </table>
          <div class="result-footer-row">
            <p class="result-footer">({lastRun?.total_orders ?? 0} rows)</p>
            {#if orderPageCount > 1}
              <div class="pager">
                <button
                  class="pager-btn"
                  type="button"
                  onclick={() => (resultPage = Math.max(0, shownPage - 1))}
                  disabled={shownPage === 0}
                  aria-label="newer rows"
                >
                  &#8249;
                </button>
                <span class="pager-count">{shownPage + 1}/{orderPageCount}</span
                >
                <button
                  class="pager-btn"
                  type="button"
                  onclick={() =>
                    (resultPage = Math.min(orderPageCount - 1, shownPage + 1))}
                  disabled={shownPage >= orderPageCount - 1}
                  aria-label="older rows"
                >
                  &#8250;
                </button>
              </div>
            {/if}
          </div>
        </div>
      {/if}
    </div>
  </div>
</section>

<style>
  /* Styled in the /ember mini-site language (lib/public/ember/ember.css, the
     fcstory palette): white panels on the warm ground, 1px hairlines, soft
     layered shadows, ember/frost accents, mono micro-labels. The page wraps
     this component in .ember-site, which provides every var(--em-*) below. */
  .pg-panel {
    display: grid;
    grid-template-columns: 340px 1fr;
    align-items: stretch;
    gap: 16px;
    max-width: 1100px;
  }

  @media (max-width: 900px) {
    .pg-panel {
      grid-template-columns: 1fr;
    }

    .result-card {
      min-height: 320px;
    }
  }

  .left-col {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .right-col {
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-width: 0;
  }

  .soft-error {
    margin: 0;
    color: var(--em-ember-deep);
    font-size: 12px;
  }

  .turnstile-slot {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow-soft);
    padding: 12px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .turnstile-hint {
    margin: 0;
    font-size: 12.5px;
    color: var(--em-muted);
  }

  .controls {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .run-btn,
  .aggregate-btn {
    padding: 11px 18px;
    border-radius: 10px;
    font-family: inherit;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    min-width: 100%;
    box-sizing: border-box;
    transition:
      background-color 0.15s ease,
      border-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .run-btn {
    background: var(--em-ember);
    border: 1px solid var(--em-ember-deep);
    color: var(--em-on-color);
    box-shadow: var(--em-shadow-soft);
  }

  .run-btn:hover:not(:disabled) {
    background: var(--em-ember-deep);
  }

  .aggregate-btn {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    color: var(--em-ink);
  }

  .aggregate-btn:hover:not(:disabled) {
    border-color: var(--em-faint);
  }

  .run-btn:disabled,
  .aggregate-btn:disabled {
    opacity: 0.55;
    cursor: default;
  }

  .run-btn,
  .aggregate-btn {
    position: relative;
  }

  .queue-chip {
    position: absolute;
    right: 12px;
    top: 50%;
    transform: translateY(-50%);
    padding: 1px 8px;
    border-radius: 999px;
    font-family: var(--em-mono);
    font-size: 11px;
    font-weight: 600;
    background: color-mix(in srgb, var(--em-panel) 30%, transparent);
    border: 1px solid color-mix(in srgb, var(--em-panel) 55%, transparent);
  }

  .fade-swap {
    display: inline-block;
  }

  .aggregate-btn .queue-chip {
    background: var(--em-ground);
    border-color: var(--em-line);
    color: var(--em-muted);
  }

  .run-error {
    margin: 0;
    color: var(--em-ember-deep);
    font-size: 13px;
  }

  .stats-card {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow-soft);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .stats-section {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .stats-best {
    border-top: 1px solid var(--em-line-soft);
    padding-top: 14px;
    gap: 8px;
  }

  .stats-label {
    font-size: 15px;
    font-weight: 750;
    letter-spacing: -0.01em;
    color: var(--em-ink);
  }

  .spark-wrap {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-top: 6px;
  }

  .spark {
    display: flex;
    align-items: flex-end;
    gap: 3px;
    height: 40px;
    border-bottom: 1px solid var(--em-line-soft);
  }

  .spark-bar {
    flex: 1 1 0;
    max-width: 10px;
    border-radius: 2px 2px 0 0;
    background: var(--em-ember);
  }

  .spark-bar.spark-cold {
    background: var(--em-ember-deep);
  }

  .spark-bar.spark-warm {
    background: var(--em-amber);
  }

  .spark-bar.spark-unclear {
    background: var(--em-faint);
  }

  .spark-caption {
    font-family: var(--em-mono);
    font-size: 10.5px;
    letter-spacing: 0.04em;
    color: var(--em-faint);
  }

  .stat-row {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
  }

  .stat-name {
    font-size: 14px;
    color: var(--em-muted);
  }

  .stat-value {
    font-family: var(--em-mono);
    font-size: 16px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--em-ink);
  }

  .stat-value.stat-live {
    color: var(--em-ember);
  }

  .stat-phase {
    margin-left: 6px;
    color: var(--em-muted);
    font-family: var(--em-mono);
    font-size: 11px;
    font-weight: 400;
  }

  .stat-note {
    margin: -4px 0 0;
    color: var(--em-faint);
    font-size: 11px;
    line-height: 1.35;
  }

  .console-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 16px;
    padding: 2px 4px;
    min-height: 22px;
  }

  .formula-bar {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-muted);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .reset-countdown {
    margin: 0;
    font-size: 12px;
    color: var(--em-faint);
    white-space: nowrap;
  }

  .reset-countdown-value {
    display: inline-block;
    min-width: 5ch;
    font-family: var(--em-mono);
    font-variant-numeric: tabular-nums;
    font-weight: 600;
    color: var(--em-muted);
  }

  .reset-countdown-clock {
    color: var(--em-faint);
  }

  .result-card {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow);
    padding: 14px 16px;
    flex: 1;
    min-height: 430px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
  }

  .result-view {
    flex: 1;
    min-height: 0;
  }

  .result-footer-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: auto;
    padding-top: 8px;
  }

  .pager {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .pager-btn {
    width: 26px;
    height: 26px;
    border-radius: 8px;
    border: 1px solid var(--em-line);
    background: var(--em-panel);
    color: var(--em-ink);
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
  }

  .pager-btn:hover:not(:disabled) {
    border-color: var(--em-faint);
  }

  .pager-btn:disabled {
    opacity: 0.4;
    cursor: default;
  }

  .pager-count {
    font-family: var(--em-mono);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    color: var(--em-muted);
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
    font-family: var(--em-mono);
  }

  .result-table th {
    text-align: left;
    padding: 4px 10px 8px 0;
    border-bottom: 1px solid var(--em-line);
    vertical-align: bottom;
  }

  .result-table th.col-numeric {
    text-align: right;
  }

  .col-name {
    display: block;
    font-size: 12px;
    text-transform: lowercase;
    color: var(--em-ink);
    font-weight: 700;
  }

  .col-type {
    display: block;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--em-faint);
    font-weight: 400;
  }

  .result-table td {
    padding: 6px 10px 6px 0;
    border-bottom: 1px solid var(--em-line-soft);
    font-variant-numeric: tabular-nums;
    color: var(--em-ink);
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
    background: color-mix(in srgb, var(--em-ember-dim) 55%, transparent);
    color: var(--em-ember-deep);
    font-size: 11px;
    font-weight: 700;
    text-transform: lowercase;
  }

  .visitor-label {
    font-size: 11px;
    color: var(--em-faint);
    text-transform: lowercase;
  }

  .epoch-band td {
    padding: 6px 10px;
    font-family: var(--em-sans);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--em-muted);
    background: var(--em-line-soft);
    border-bottom: 1px solid var(--em-line);
  }

  .epoch-band.epoch-current td {
    background: color-mix(
      in srgb,
      var(--em-ember-dim) 40%,
      var(--em-line-soft)
    );
  }

  .summary-row td:first-child {
    position: relative;
  }

  .summary-bar {
    position: absolute;
    inset: 2px 0;
    background: color-mix(in srgb, var(--em-ember-dim) 45%, transparent);
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
    border-top: 2px solid var(--em-line);
    font-weight: 700;
    padding-top: 10px;
  }

  .result-footer {
    margin: 0;
    font-size: 12px;
    font-family: var(--em-mono);
    color: var(--em-faint);
  }

  @media (prefers-reduced-motion: no-preference) {
    .row-new {
      animation: row-new-fade 0.8s ease-out;
    }

    @keyframes row-new-fade {
      0% {
        background: color-mix(in srgb, var(--em-ember-dim) 60%, transparent);
      }
      100% {
        background: transparent;
      }
    }
  }
</style>
