<script>
  // /ember/bazel: the frozen-Skyframe query exhibit (ADR embervm/010).
  // A real Bazel server was warmed once, its Skyframe graph resident in the
  // JVM heap after loading and analyzing Abseil (514 targets), then frozen
  // as a Firecracker memory snapshot. Every query below runs in a disposable
  // copy-on-write clone of that frozen brain: relight, one query, destroy.
  //
  // Console flow mirrors /ember/postgres's EmberConsole: Turnstile-gated
  // session mint (sessionless when no site key, i.e. dev), a stopwatch that
  // ticks live via requestAnimationFrame, and an in-band error shape split
  // between pre-submit rejections (200 + {error, ...flag}) and post-submit
  // failures (a real non-2xx status; see ember_public/bazel_router.py).
  // Unlike postgres, there is no lifecycle to poll: each query is one
  // task-class Assign, so there is no status endpoint and no wake stage.
  //
  // Same-origin proxies only (public-tier rule 2): every call below goes
  // through /ember/bazel/api/*, never /api/... directly.
  import { onMount } from "svelte";
  import { fade } from "svelte/transition";
  import "$lib/public/ember/ember.css";

  let { data } = $props();

  const API = "/ember/bazel/api";

  const DEFAULT_EXPR = "deps(//absl/strings)";
  const EXAMPLES = [
    { label: "cc_library kinds", expr: 'kind("cc_library", //absl/...)' },
    { label: "somepath", expr: "somepath(//absl/base, //absl/time)" },
  ];

  const PROOF_MARKER = "0 packages loaded, 0 targets configured";

  let expr = $state(DEFAULT_EXPR);
  let running = $state(false);
  let stopwatchMs = $state(0);
  let result = $state(null);
  let runError = $state("");

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

  function ms(v) {
    if (v == null) return "–";
    return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
  }

  // Session gate: sessionReady flips true once /session mints (or confirms)
  // a cookie. Sessionless-mint mode (no site key) starts true only after the
  // fire-and-forget mint on mount resolves ok, matching the postgres console.
  let turnstileSiteKey = data.turnstileSiteKey ?? "";
  let sessionReady = $state(false);
  let sessionError = $state("");
  let queryUnavailable = $state(false);

  async function parseJsonSafe(resp) {
    const text = await resp.text();
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  }

  async function mintSession(turnstileToken = "") {
    try {
      const resp = await fetch(`${API}/session`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ turnstile_token: turnstileToken }),
      });
      if (resp.status === 429) return;
      const body = (await parseJsonSafe(resp)) ?? {};
      if (resp.ok && body.ok) {
        sessionReady = true;
        sessionError = "";
        queryUnavailable = false;
      } else if (!resp.ok) {
        if (turnstileSiteKey) {
          sessionError = "verification failed, try again";
        } else {
          queryUnavailable = true;
        }
      }
    } catch {
      // fire-and-forget: a network hiccup just leaves querying gated
    }
  }

  // Client-side cooldown so repeat clicks cannot outrun the backend's own
  // one-query-per-3s session bucket; a click during the gap queues (capped).
  const COOLDOWN_MS = 3200;
  let cooldown = $state(false);
  let cooldownTimer = null;
  let queuedExpr = $state(null);

  function startCooldown() {
    cooldown = true;
    if (cooldownTimer) clearTimeout(cooldownTimer);
    cooldownTimer = setTimeout(() => {
      cooldown = false;
      cooldownTimer = null;
      if (queuedExpr != null) {
        const next = queuedExpr;
        queuedExpr = null;
        runQuery(next);
      }
    }, COOLDOWN_MS);
  }

  function requestRun(runExpr) {
    if (!!turnstileSiteKey && !sessionReady) return;
    if (queryUnavailable) return;
    if (running || cooldown) {
      queuedExpr = runExpr;
      return;
    }
    runQuery(runExpr);
  }

  async function runQuery(runExpr) {
    if (running) return;
    running = true;
    runError = "";
    startStopwatch();
    try {
      const resp = await fetch(`${API}/query`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ expression: runExpr }),
      });
      const body = await parseJsonSafe(resp);
      if (body == null) {
        runError = `unexpected response from the demo (${resp.status})`;
        return;
      }
      if (resp.status === 429) {
        runError = "the demo is busy right now, give it a few seconds and try again";
        return;
      }
      if (!resp.ok) {
        // Post-submit failures come back as a real HTTP status with a
        // FastAPI {"detail": ...} body (see ember_public/bazel_router.py's
        // HTTPException on non-200 from run_query): the guest's own 422 with
        // bazel's verbatim error text, or a 502/504 from a transport error
        // or timeout. Pre-submit rejections (bad expression, missing
        // session, rate limit, busy semaphore) are handled below as in-band
        // 200 + {error, ...flag} bodies instead.
        runError = body?.detail ?? body?.error ?? `query failed (${resp.status})`;
        return;
      }
      if (body.session_required) {
        if (!turnstileSiteKey) queryUnavailable = true;
        runError = turnstileSiteKey
          ? "solve the check above to query"
          : "querying needs the human check, which is not configured here";
        return;
      }
      if (body.rate_limited) {
        runError = "one query per few seconds, wait a moment and retry";
        return;
      }
      if (body.busy) {
        runError = "both clones are busy right now, try again in a moment";
        return;
      }
      if (body.error) {
        // A real bazel error (from the guest's own 422, or a validation
        // rejection) shown verbatim: a typo'd query is a feature here, not
        // a bug, visitors see bazel's actual error text.
        runError = body.error;
        return;
      }
      result = body;
    } catch (err) {
      runError = String(err);
    } finally {
      running = false;
      stopStopwatch();
      startCooldown();
    }
  }

  onMount(() => {
    if (!turnstileSiteKey) {
      mintSession("");
    }
    return () => {
      stopStopwatch();
      if (cooldownTimer) clearTimeout(cooldownTimer);
    };
  });

  // Turnstile widget lifecycle: mirrors EmberConsole's script-load +
  // render-once + remove-on-unmount pattern.
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

  // Highlight the proof marker inside analyzed_line for the badge. Falls
  // back to the raw line (no highlight span) if the marker is missing,
  // which would itself be the drift condition the backend logs a warning
  // for (ember_public/bazel_core.py _check_drift).
  let analyzedParts = $derived.by(() => {
    const line = result?.analyzed_line ?? "";
    const idx = line.indexOf(PROOF_MARKER);
    if (idx === -1) return { before: line, marker: "", after: "" };
    return {
      before: line.slice(0, idx),
      marker: line.slice(idx, idx + PROOF_MARKER.length),
      after: line.slice(idx + PROOF_MARKER.length),
    };
  });

  let labelLines = $derived((result?.labels ?? "").split("\n").filter((l) => l.length > 0));
</script>

<svelte:head>
  <title>Ember Bazel Skyframe Query</title>
  <meta
    name="description"
    content="Query a warm Bazel analysis graph frozen as a Firecracker memory snapshot. Each query runs in a disposable clone: relight, cquery, destroy. Bazel's own output proves zero re-analysis happened."
  />
</svelte:head>

<div class="ember-site">
  <header class="topbar">
    <span
      ><a class="brand" href="/"><strong>jomcgi.dev</strong></a> /
      <a class="brand" href="/ember">ember</a> / bazel</span
    >
    <a class="topbar-cross" href="/ember/postgres">see the postgres exhibit</a>
  </header>

  <main class="ember-page">
    <header class="masthead">
      <h1><span class="ember-word">Ember</span> Bazel Skyframe Query</h1>
      <p class="subtitle">
        A real Bazel server was warmed once: loading and analysis of Abseil,
        514 targets. That warm server was frozen as a Firecracker memory
        snapshot. Every query below runs in a disposable copy-on-write clone
        of that frozen brain: relight, one query, destroy.
      </p>
    </header>

    <section class="console-section">
      {#if turnstileSiteKey && !sessionReady}
        <div class="turnstile-slot">
          <p class="turnstile-hint">solve the check to query</p>
          <div bind:this={widgetEl} class="turnstile-widget"></div>
          {#if sessionError}
            <p class="soft-error">{sessionError}</p>
          {/if}
        </div>
      {/if}

      <div class="query-bar">
        <input
          class="query-input"
          type="text"
          bind:value={expr}
          spellcheck="false"
          autocomplete="off"
          placeholder={DEFAULT_EXPR}
        />
        <button
          class="run-btn"
          type="button"
          onclick={() => requestRun(expr)}
          disabled={(!!turnstileSiteKey && !sessionReady) || queryUnavailable}
        >
          {queryUnavailable ? "unavailable" : running ? "querying…" : "run cquery"}
        </button>
      </div>

      <div class="chips">
        {#each EXAMPLES as ex (ex.expr)}
          <button
            class="chip"
            type="button"
            onclick={() => {
              expr = ex.expr;
              requestRun(ex.expr);
            }}
          >
            {ex.label}
          </button>
        {/each}
      </div>

      <div class="stopwatch-row">
        <span class="stopwatch-label">round trip</span>
        <span class="stopwatch-value" class:stopwatch-live={running}>
          {#key running ? "live" : (result?.wall_ms ?? "none")}
            <span class="fade-swap" in:fade={{ duration: 220 }}>
              {running ? ms(stopwatchMs) : ms(result?.wall_ms)}
            </span>
          {/key}
        </span>
      </div>

      {#if runError}
        <div class="run-error">
          <span class="run-error-label">bazel says:</span>
          <pre class="run-error-text">{runError}</pre>
        </div>
      {/if}

      {#if result}
        {#if analyzedParts.before || analyzedParts.marker}
          <div class="proof-badge">
            <span class="proof-kicker">proof of reuse, bazel's own words</span>
            <p class="proof-line">
              {analyzedParts.before}{#if analyzedParts.marker}<b class="proof-marker">{analyzedParts.marker}</b>{/if}{analyzedParts.after}
            </p>
            <p class="proof-caption">
              Zero packages loaded, zero targets configured: bazel's own
              admission that no re-analysis happened. This clone answered
              the query by reusing the Skyframe graph restored straight from
              the frozen heap.
            </p>
          </div>
        {/if}

        <div class="result-card">
          <div class="result-header">
            <span class="result-count">{labelLines.length} label{labelLines.length === 1 ? "" : "s"}</span>
            {#if result.truncated}
              <span class="result-truncated">output truncated</span>
            {/if}
          </div>
          <ul class="label-list">
            {#each labelLines as label, i (i)}
              <li>{label}</li>
            {/each}
          </ul>
        </div>
      {/if}
    </section>

    <section class="recorded-section">
      <h2 class="h2">Cold, warm, and live</h2>
      <p class="body">
        Three numbers, three different things measured. The first two are a
        recorded comparison from before the snapshot existed; the third is
        what actually happens on your query above.
      </p>
      <div class="recorded-panel">
        <div class="recorded-stat">
          <span class="recorded-value recorded-cold">13.8 s</span>
          <span class="recorded-name">cold: loading + analysis</span>
          <span class="recorded-note">warm dev server, macOS, 10 cores, pre-snapshot</span>
        </div>
        <div class="recorded-stat">
          <span class="recorded-value recorded-warm">0.31 s</span>
          <span class="recorded-name">warm: cquery on an already-loaded server</span>
          <span class="recorded-note">same machine, same run, cache hot</span>
        </div>
        <div class="recorded-stat">
          <span class="recorded-value recorded-live">~450 ms</span>
          <span class="recorded-name">live: one visitor query, end to end</span>
          <span class="recorded-note"
            >measured on the production node tonight, includes clone
            relight and reap, ~300&nbsp;ms of it inside bazel</span
          >
        </div>
      </div>
      <p class="recorded-footer">
        Design doc: <a
          href="https://github.com/jomcgi/homelab/blob/main/docs/decisions/embervm/010-bazel-skyframe-snapshot-query-demo.md"
          >ADR embervm/010</a
        >.
      </p>
    </section>
  </main>
</div>

<style>
  .ember-site {
    min-height: 100dvh;
  }

  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 14px 28px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-muted);
  }

  .topbar strong {
    color: var(--em-ink);
    font-weight: 600;
  }

  .topbar .brand {
    color: inherit;
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar .brand:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .topbar .brand:focus-visible,
  .topbar-cross:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .topbar-cross {
    color: var(--em-muted);
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar-cross:hover {
    color: var(--em-ember-deep);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .ember-page {
    max-width: 900px;
    margin: 0 auto;
    padding: 4px 24px 64px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .masthead {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-bottom: 4px;
  }

  .masthead h1 {
    margin: 0;
    font-size: clamp(24px, 2.4vw, 30px);
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.1;
    color: var(--em-ink);
  }

  .ember-word {
    color: var(--em-ember);
  }

  .subtitle {
    margin: 0;
    font-size: 14.5px;
    line-height: 1.5;
    color: var(--em-muted);
    max-width: 68ch;
  }

  .console-section {
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 14px;
    box-shadow: var(--em-shadow);
    padding: 18px 20px;
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-top: 8px;
  }

  .turnstile-slot {
    background: var(--em-ground);
    border: 1px solid var(--em-line);
    border-radius: 12px;
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

  .soft-error {
    margin: 0;
    color: var(--em-ember-deep);
    font-size: 12px;
  }

  .query-bar {
    display: flex;
    gap: 10px;
  }

  .query-input {
    flex: 1;
    min-width: 0;
    padding: 11px 14px;
    border-radius: 10px;
    border: 1px solid var(--em-line);
    background: var(--em-ground);
    color: var(--em-ink);
    font-family: var(--em-mono);
    font-size: 13.5px;
  }

  .query-input:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 1px;
  }

  .run-btn {
    flex: none;
    padding: 11px 20px;
    border-radius: 10px;
    font-family: inherit;
    font-weight: 600;
    font-size: 14px;
    cursor: pointer;
    background: var(--em-ember);
    border: 1px solid var(--em-ember-deep);
    color: var(--em-on-color);
    box-shadow: var(--em-shadow-soft);
    transition:
      background-color 0.15s ease,
      box-shadow 0.15s ease;
  }

  .run-btn:hover:not(:disabled) {
    background: var(--em-ember-deep);
  }

  .run-btn:disabled {
    opacity: 0.55;
    cursor: default;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .chip {
    padding: 6px 12px;
    border-radius: 999px;
    border: 1px solid var(--em-line);
    background: var(--em-ground);
    color: var(--em-muted);
    font-family: var(--em-mono);
    font-size: 12px;
    cursor: pointer;
    transition:
      border-color 0.15s ease,
      color 0.15s ease;
  }

  .chip:hover {
    border-color: var(--em-ember-dim);
    color: var(--em-ember-deep);
  }

  .stopwatch-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    padding-top: 2px;
  }

  .stopwatch-label {
    font-size: 13px;
    color: var(--em-muted);
  }

  .stopwatch-value {
    font-family: var(--em-mono);
    font-size: 16px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--em-ink);
  }

  .stopwatch-value.stopwatch-live {
    color: var(--em-ember);
  }

  .fade-swap {
    display: inline-block;
  }

  .run-error {
    background: color-mix(in srgb, var(--em-ember-dim) 25%, var(--em-ground));
    border: 1px solid var(--em-ember-dim);
    border-radius: 10px;
    padding: 10px 14px;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .run-error-label {
    font-family: var(--em-mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--em-ember-deep);
  }

  .run-error-text {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 12.5px;
    line-height: 1.5;
    color: var(--em-ink);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .proof-badge {
    background: color-mix(in srgb, var(--em-ember-dim) 35%, var(--em-panel));
    border: 1px solid var(--em-ember-dim);
    border-radius: 12px;
    padding: 16px 18px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .proof-kicker {
    font-family: var(--em-mono);
    font-size: 11px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--em-ember-deep);
    font-weight: 600;
  }

  .proof-line {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 13.5px;
    line-height: 1.5;
    color: var(--em-ink);
    word-break: break-word;
  }

  .proof-marker {
    background: var(--em-ember);
    color: var(--em-on-color);
    border-radius: 4px;
    padding: 1px 6px;
    font-weight: 700;
  }

  .proof-caption {
    margin: 0;
    font-size: 13px;
    line-height: 1.5;
    color: var(--em-muted);
  }

  .result-card {
    border: 1px solid var(--em-line);
    border-radius: 12px;
    background: var(--em-ground);
    padding: 12px 16px;
    max-height: 360px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .result-header {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-faint);
  }

  .result-truncated {
    color: var(--em-ember-deep);
  }

  .label-list {
    margin: 0;
    padding: 0;
    list-style: none;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-ink);
  }

  .label-list li {
    padding: 2px 0;
    border-bottom: 1px solid var(--em-line-soft);
    word-break: break-all;
  }

  .label-list li:last-child {
    border-bottom: none;
  }

  .recorded-section {
    margin-top: 12px;
  }

  .h2 {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin: 0 0 10px;
    font-size: 20px;
    font-weight: 750;
    letter-spacing: -0.015em;
    color: var(--em-ink);
  }

  .h2::before {
    content: "##";
    font-family: var(--em-mono);
    font-size: 14px;
    font-weight: 400;
    color: var(--em-ember);
  }

  .body {
    margin: 0 0 14px;
    font-size: 14.5px;
    line-height: 1.55;
    color: var(--em-muted);
    max-width: 68ch;
  }

  .recorded-panel {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
    background: var(--eml-panel-warm, var(--em-panel));
    border: 1px solid var(--em-line);
    border-radius: 12px;
    box-shadow: var(--em-shadow-soft);
    padding: 18px;
  }

  .recorded-stat {
    display: flex;
    flex-direction: column;
    gap: 4px;
  }

  .recorded-value {
    font-family: var(--em-mono);
    font-size: 26px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .recorded-cold {
    color: var(--em-frost);
  }

  .recorded-warm {
    color: var(--em-amber);
  }

  .recorded-live {
    color: var(--em-ember-deep);
  }

  .recorded-name {
    font-size: 13px;
    font-weight: 600;
    color: var(--em-ink);
  }

  .recorded-note {
    font-size: 11.5px;
    line-height: 1.4;
    color: var(--em-faint);
  }

  .recorded-footer {
    margin: 12px 2px 0;
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-faint);
  }

  .recorded-footer a {
    color: var(--em-ember-deep);
    text-decoration: none;
    border-bottom: 1px solid var(--em-ember-dim);
  }

  .recorded-footer a:hover {
    border-bottom-color: var(--em-ember-deep);
  }

  @media (max-width: 900px) {
    .topbar {
      padding: 12px 16px;
    }

    .ember-page {
      padding: 4px 16px 48px;
      gap: 14px;
    }

    .recorded-panel {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 560px) {
    .query-bar {
      flex-direction: column;
    }
  }
</style>
