<script>
  // /ember/bazel: the warm-Skyframe query demo (ADR embervm/010).
  // A Bazel server was warmed once, its Skyframe graph resident in the JVM heap
  // after loading and analyzing Abseil (514 targets), then snapshotted with
  // Firecracker. Every query below runs in its own throwaway copy of that
  // snapshot: restore the copy, run one query, destroy it.
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
  import "$lib/public/ember/ember.css";

  let { data } = $props();

  const API = "/ember/bazel/api";

  const DEFAULT_EXPR = "deps(//absl/strings)";
  // Known-good cquery expressions offered as one-click chips. Each is verified
  // against the backend charset validator (bazel_core.validate_expr) and is a
  // real Abseil target/pattern, so running one always returns a result, never
  // an error. Clicking a chip only fills the input; the visitor runs it with
  // the button (so they can eyeball or tweak the expression first).
  const EXAMPLES = [
    { label: "deps", expr: "deps(//absl/strings)" },
    { label: "cc_library kinds", expr: 'kind("cc_library", //absl/...)' },
    { label: "somepath", expr: "somepath(//absl/base, //absl/time)" },
    { label: "reverse deps", expr: "rdeps(//absl/..., //absl/base:config)" },
    { label: "package labels", expr: "//absl/hash/..." },
  ];

  const PROOF_MARKER = "0 packages loaded, 0 targets configured";

  let expr = $state(DEFAULT_EXPR);
  let running = $state(false);
  let stopwatchMs = $state(0);
  let result = $state(null);
  let runError = $state("");
  // A rejected query (wrong cquery) still ran against the warm snapshot, so the
  // backend hands back the failed run's wall_ms. Hold it here so the scoreboard
  // shows the timing even though `result` stays null on an error.
  let errorWallMs = $state(null);

  // All-time "estimated cold analysis time skipped" counter (see
  // ember_public/bazel_router.py GET /savings). SSR-seeded so the counter
  // isn't blank on first paint, refetched after every successful query so
  // the visitor watches their own contribution land without a reload.
  let savedS = $state(data.initialSavings?.total_analysis_s_saved ?? null);

  async function refetchSavings() {
    try {
      const resp = await fetch(`${API}/savings`);
      if (!resp.ok) return;
      const body = await parseJsonSafe(resp);
      if (body?.total_analysis_s_saved != null)
        savedS = body.total_analysis_s_saved;
    } catch {
      // best-effort: leave the last known value on screen
    }
  }

  function formatSavedTime(s) {
    if (s == null) return "–";
    if (s < 60) return `${s.toFixed(1)} s`;
    if (s < 3600) return `${(s / 60).toFixed(1)} min`;
    return `${(s / 3600).toFixed(1)} h`;
  }

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
    // Clear the previous result/proof/labels up front so a new run that errors
    // does not leave a stale success panel (and its green proof badge) rendered
    // underneath the error box. Filter text resets with it.
    result = null;
    errorWallMs = null;
    filterText = "";
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
        runError =
          "the demo is busy right now, give it a few seconds and try again";
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
        runError =
          body?.detail ?? body?.error ?? `query failed (${resp.status})`;
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
        runError = "The demo is busy right now. Try again in a moment.";
        return;
      }
      if (body.error) {
        // A real bazel error (from the guest's own 422, or a validation
        // rejection) shown verbatim: a typo'd query is a feature here, not
        // a bug, visitors see bazel's actual error text.
        runError = body.error;
        // A query bazel actually evaluated and rejected still ran against the
        // warm snapshot, so the backend returns its real wall_ms and credits
        // the skipped cold analysis. Surface both: the failed run's timing
        // lands in the "your query" cell and the shared counter ticks up. A
        // pre-flight validation reject carries no wall_ms (nothing ran).
        if (typeof body.wall_ms === "number" && body.wall_ms > 0) {
          errorWallMs = body.wall_ms;
          refetchSavings();
        }
        return;
      }
      result = body;
      // Fire-and-forget: the backend already credited this query's savings
      // synchronously inside POST /query, this just re-reads the counter so
      // the visitor sees their own contribution without a page reload. A
      // failed refetch just leaves the last known value on screen.
      refetchSavings();
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
    if (savedS == null) refetchSavings();
    return () => {
      stopStopwatch();
      if (cooldownTimer) clearTimeout(cooldownTimer);
    };
  });

  // Turnstile widget lifecycle: mirrors EmberConsole's script-load +
  // render-once + remove-on-unmount pattern.
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

  // Highlight the proof marker inside analyzed_line for the proof line. Falls
  // back to the raw line (no highlight span) if the marker is missing, which
  // would itself be the drift condition the backend logs a warning for
  // (ember_public/bazel_core.py _check_drift). The pill swallows the wrapping
  // parens too when they are present, so it reads "(0 packages loaded, 0
  // targets configured)" as one fragment, matching bazel's own line.
  let analyzedParts = $derived.by(() => {
    const line = result?.analyzed_line ?? "";
    const idx = line.indexOf(PROOF_MARKER);
    if (idx === -1) return { before: line, marker: "", after: "" };
    let start = idx;
    let end = idx + PROOF_MARKER.length;
    if (line[start - 1] === "(" && line[end] === ")") {
      start -= 1;
      end += 1;
    }
    return {
      before: line.slice(0, start),
      marker: line.slice(start, end),
      after: line.slice(end),
    };
  });

  // cquery's frozen --output=label format is "//pkg:target (config-hash)" or
  // "//pkg:target (null)" for source files, which have no configuration.
  // Guest flags stay frozen (warming/serving parity), so this is purely a
  // client-side render split: strip the trailing " (...)" and keep the
  // config hash as a separate field, rendering nothing for a null config
  // rather than the literal text "(null)", which reads as an error.
  const LABEL_CONFIG_RE = /^(.*) \(([^()]*)\)$/;

  function parseLabelLine(line) {
    const m = LABEL_CONFIG_RE.exec(line);
    if (!m) return { target: line, configHash: null };
    const [, target, config] = m;
    return { target, configHash: config === "null" ? null : config };
  }

  let labelItems = $derived(
    (result?.labels ?? "")
      .split("\n")
      .filter((l) => l.length > 0)
      .map(parseLabelLine),
  );

  // Client-side substring filter + pagination: the full list is already in
  // memory (capped at 256KiB server-side), so both are plain array ops, no
  // round trip. Filter resets to page 1 whenever the query text or the
  // underlying result changes.
  const PAGE_SIZE = 25;
  let filterText = $state("");
  let labelPage = $state(0);

  let filteredLabelItems = $derived.by(() => {
    const needle = filterText.trim().toLowerCase();
    if (!needle) return labelItems;
    return labelItems.filter((item) =>
      item.target.toLowerCase().includes(needle),
    );
  });

  let labelPageCount = $derived(
    Math.max(1, Math.ceil(filteredLabelItems.length / PAGE_SIZE)),
  );

  let shownLabelPage = $derived(Math.min(labelPage, labelPageCount - 1));

  let pagedLabelItems = $derived.by(() => {
    const page = shownLabelPage;
    return filteredLabelItems.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE);
  });

  $effect(() => {
    // Reset to page 1 whenever the filter text or the result set changes,
    // not on every re-render (an $effect over the exact reset triggers,
    // rather than doing it inline in the filter handler, keeps the "new
    // result" reset and the "typed a filter" reset in one place).
    filterText;
    labelItems;
    labelPage = 0;
  });
</script>

<svelte:head>
  <title>Ember Bazel Skyframe Query</title>
  <meta
    name="description"
    content="Bazel's analysis graph (Skyframe) lives only in server memory, so every cold start recomputes it. Here it was computed once for Abseil (514 targets), snapshotted with Firecracker, and every query restores a throwaway copy."
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
      <p class="lede">
        Ask a question about a 514-target C++ build graph and get the answer in
        under a second, from a build server that was frozen mid-thought.
      </p>
      <p class="subtitle">
        <a class="inline-link" href="https://github.com/abseil/abseil-cpp"
          >Abseil</a
        >
        (514 targets) was analyzed once and the warm server snapshotted with Firecracker.
        Every query below restores a throwaway copy.
      </p>
    </header>

    <section class="console-section">
      <!-- Scoreboard: the core comparison as a 4-cell header banded across the
           top edge of the console card. Two recorded baselines (cold, warm),
           this session's live round trip (an em-dash until the first query),
           and the all-time savings counter on its own green cell. Numbers are
           tabular so digits do not jitter as the live cell ticks. -->
      <div class="scoreboard">
        <div class="score-cell">
          <div
            class="score-v score-cold"
            title="measured on a warm dev machine, before the snapshot existed"
          >
            13.8 s
          </div>
          <div class="score-k">cold, recorded</div>
        </div>
        <div class="score-cell">
          <div
            class="score-v score-warm"
            title="measured on a warm dev machine, before the snapshot existed"
          >
            0.31 s
          </div>
          <div class="score-k">warm, recorded</div>
        </div>
        <div class="score-cell">
          <div
            class="score-v score-live"
            class:score-live-running={running}
            title="end to end, includes restoring and destroying the copy; about 300 ms of it is bazel"
          >
            {#if running}
              {ms(stopwatchMs)}
            {:else if result?.wall_ms != null}
              {ms(result.wall_ms)}
            {:else if errorWallMs != null}
              {ms(errorWallMs)}
            {:else}
              &mdash;
            {/if}
          </div>
          <div class="score-k">your query</div>
        </div>
        <div class="score-cell score-cell-save">
          <div class="score-v score-save">{formatSavedTime(savedS)}</div>
          <div class="score-k">skipped, all visitors</div>
        </div>
      </div>

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
          class:is-pending={running || queuedExpr != null}
          type="button"
          onclick={() => requestRun(expr)}
          disabled={(!!turnstileSiteKey && !sessionReady) || queryUnavailable}
        >
          {queryUnavailable
            ? "unavailable"
            : running
              ? "querying…"
              : queuedExpr != null
                ? "queued…"
                : "run cquery"}
        </button>
      </div>

      <div class="chips">
        {#each EXAMPLES as ex (ex.expr)}
          <button
            class="chip"
            type="button"
            onclick={() => {
              expr = ex.expr;
            }}
          >
            {ex.label}
          </button>
        {/each}
      </div>

      {#if !running && !result && !runError}
        <p class="idle-hint">pick an example or type your own, then run</p>
      {/if}

      {#if runError}
        <div class="run-error">
          <span class="run-error-label">bazel says:</span>
          <pre class="run-error-text">{runError}</pre>
        </div>
      {/if}

      {#if result}
        {#if analyzedParts.before || analyzedParts.marker}
          <div class="proof">
            <p class="proof-line">
              <span class="proof-check" aria-hidden="true">✓</span
              >{analyzedParts.before}{#if analyzedParts.marker}<span
                  class="proof-frag">{analyzedParts.marker}</span
                >{/if}{analyzedParts.after}
            </p>
            <p class="proof-caption">
              no re-analysis, straight from the snapshot's memory
            </p>
          </div>
        {/if}

        <div class="result-card">
          <div class="result-header">
            <span class="result-count">
              {filteredLabelItems.length}
              {#if filteredLabelItems.length !== labelItems.length}of {labelItems.length}{/if}
              label{labelItems.length === 1 ? "" : "s"}
            </span>
            {#if result.truncated}
              <span class="result-truncated">output truncated</span>
            {/if}
          </div>

          <input
            class="label-filter"
            type="text"
            bind:value={filterText}
            placeholder="filter labels"
            spellcheck="false"
            autocomplete="off"
          />

          <ul class="label-list">
            {#each pagedLabelItems as item, i (i)}
              <li>
                <span class="label-target">{item.target}</span>
                {#if item.configHash}
                  <span
                    class="label-config-badge"
                    title="the target's configuration hash; source files have none"
                    >{item.configHash}</span
                  >
                {/if}
              </li>
            {:else}
              <li class="label-empty">no labels match "{filterText}"</li>
            {/each}
          </ul>

          {#if labelPageCount > 1}
            <div class="result-footer-row">
              <div class="pager">
                <button
                  class="pager-btn"
                  type="button"
                  onclick={() => (labelPage = Math.max(0, shownLabelPage - 1))}
                  disabled={shownLabelPage === 0}
                  aria-label="previous page"
                >
                  &#8249;
                </button>
                <span class="pager-count"
                  >page {shownLabelPage + 1} of {labelPageCount}</span
                >
                <button
                  class="pager-btn"
                  type="button"
                  onclick={() =>
                    (labelPage = Math.min(
                      labelPageCount - 1,
                      shownLabelPage + 1,
                    ))}
                  disabled={shownLabelPage >= labelPageCount - 1}
                  aria-label="next page"
                >
                  &#8250;
                </button>
              </div>
            </div>
          {/if}
        </div>
      {/if}
    </section>

    <p class="design-doc">
      Design doc: <a
        href="https://github.com/jomcgi/homelab/blob/main/docs/decisions/embervm/010-bazel-skyframe-snapshot-query-demo.md"
        >ADR embervm/010</a
      >.
    </p>
  </main>
</div>

<style>
  /* PAGE-WIDE COLOUR RULE (ADR embervm/010 demo pages): the ember red/salmon
     palette (--em-ember, --em-ember-deep, --em-ember-dim) is reserved
     EXCLUSIVELY for actual errors, i.e. the "bazel says" run-error box. Every
     POSITIVE or NEUTRAL metric (the proof line, the savings cell, the live
     latency number) uses the success-green tokens (--em-good*, defined in
     ember.css) instead, so a good result never reads as a failure.
     Brand/interactive chrome (the run button, links, chip hovers, focus rings,
     the "Ember" word) may still use ember: those are not success/failure
     signals. */
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
    gap: 8px;
    padding-bottom: 8px;
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

  /* Lede: the one-sentence hook, a touch bolder and darker than the body
     paragraph beneath it, on a tighter 58ch measure so it reads as a lede
     rather than a wide body line. */
  .lede {
    margin: 0;
    font-size: 15px;
    font-weight: 500;
    line-height: 1.5;
    color: var(--em-ink);
    max-width: 58ch;
  }

  .subtitle {
    margin: 0;
    font-size: 14.5px;
    line-height: 1.5;
    color: var(--em-muted);
    max-width: 58ch;
  }

  .inline-link {
    color: var(--em-ember-deep);
    text-decoration: none;
    border-bottom: 1px solid var(--em-ember-dim);
  }

  .inline-link:hover {
    border-bottom-color: var(--em-ember-deep);
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

  /* Scoreboard: banded across the top edge of the console card. Negative
     margins pull it flush to the card's rounded corners (cancelling the
     card's 18px/20px padding), a cream band with hairline-divided cells. */
  .scoreboard {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    margin: -18px -20px 4px;
    background: var(--em-ground);
    border-bottom: 1px solid var(--em-line);
    border-radius: 14px 14px 0 0;
    overflow: hidden;
  }

  .score-cell {
    padding: 11px 16px;
    border-left: 1px solid var(--em-line);
  }

  .score-cell:first-child {
    border-left: 0;
  }

  .score-cell-save {
    background: var(--em-good-dim);
  }

  .score-v {
    font-family: var(--em-mono);
    font-weight: 700;
    font-size: 17px;
    font-variant-numeric: tabular-nums;
  }

  .score-k {
    font-size: 12px;
    color: var(--em-faint);
    margin-top: 2px;
  }

  .score-cold {
    color: var(--em-frost);
  }

  .score-warm {
    color: var(--em-amber);
  }

  .score-live {
    color: var(--em-ink);
  }

  .score-live-running {
    /* the ticking live cell is a positive metric, so success-green while
       running, not the ember red reserved for the error box */
    color: var(--em-good-deep);
  }

  .score-save {
    color: var(--em-good-deep);
  }

  .idle-hint {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-faint);
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

  /* Same "working" cue for both states that block a query: a query in flight
     (running) and a click parked during the cooldown (queued). A steady pulse
     reads as pending without the hard-dimmed look of :disabled. */
  .run-btn.is-pending {
    cursor: default;
  }

  @media (prefers-reduced-motion: no-preference) {
    .run-btn.is-pending {
      animation: run-btn-pulse 1s ease-in-out infinite;
    }
  }

  @keyframes run-btn-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.6;
    }
  }

  /* Drop the generic blue UA focus ring left after a mouse click, but keep an
     ember-colored ring for keyboard focus so tab navigation stays visible. */
  .run-btn:focus,
  .chip:focus {
    outline: none;
  }

  .run-btn:focus-visible,
  .chip:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 2px;
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

  /* Proof of reuse: two compact green lines, not a panel. Positive result, so
     it uses the --em-good tokens (defined in ember.css), never the ember salmon
     reserved for the run-error box. Line 1 is bazel's own Analyzed sentence
     with the "(0 packages loaded, ...)" fragment in a soft green pill; line 2
     is the smaller muted gloss beneath it. */
  .proof {
    display: flex;
    flex-direction: column;
    gap: 3px;
  }

  .proof-line {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 13px;
    line-height: 1.55;
    color: var(--em-good);
    word-break: break-word;
  }

  .proof-check {
    color: var(--em-good);
    font-weight: 700;
    margin-right: 6px;
  }

  .proof-frag {
    background: var(--em-good-dim);
    color: var(--em-good-deep);
    border-radius: 5px;
    padding: 1px 7px;
  }

  .proof-caption {
    margin: 0;
    font-size: 12px;
    line-height: 1.5;
    color: var(--em-faint);
  }

  .result-card {
    border: 1px solid var(--em-line);
    border-radius: 12px;
    background: var(--em-ground);
    padding: 12px 16px;
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
    /* an informational note (the label list was capped), not an error: neutral
       faint tone, not the ember red reserved for the error box */
    color: var(--em-faint);
  }

  .label-filter {
    padding: 7px 12px;
    border-radius: 8px;
    border: 1px solid var(--em-line);
    background: var(--em-panel);
    color: var(--em-ink);
    font-family: var(--em-mono);
    font-size: 12.5px;
  }

  .label-filter:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 1px;
  }

  .label-list {
    margin: 0;
    padding: 0;
    list-style: none;
    /* Well below viewport height: with pagination capping each page at 25
       rows, this is a safety net for unusually long wrapped label text, not
       the primary scroll mechanism (that's the pager below). */
    max-height: min(40vh, 320px);
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-ink);
  }

  .label-list li {
    padding: 3px 0;
    border-bottom: 1px solid var(--em-line-soft);
    display: flex;
    align-items: baseline;
    gap: 8px;
  }

  .label-list li:last-child {
    border-bottom: none;
  }

  .label-target {
    word-break: break-all;
    flex: 1;
    min-width: 0;
  }

  .label-config-badge {
    flex: none;
    font-size: 10.5px;
    color: var(--em-faint);
    background: var(--em-line-soft);
    border-radius: 999px;
    padding: 1px 7px;
    font-variant-numeric: tabular-nums;
  }

  .label-empty {
    color: var(--em-faint);
    font-style: italic;
    border-bottom: none;
  }

  .result-footer-row {
    display: flex;
    justify-content: flex-end;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
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

  /* The only thing below the console card: a single small design-doc line. */
  .design-doc {
    margin: 0 2px;
    font-family: var(--em-mono);
    font-size: 12px;
    color: var(--em-faint);
  }

  .design-doc a {
    color: var(--em-ember-deep);
    text-decoration: none;
    border-bottom: 1px solid var(--em-ember-dim);
  }

  .design-doc a:hover {
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
  }

  @media (max-width: 640px) {
    /* Two-up scoreboard on narrow screens: four thin cells would clip the
       tabular figures, so wrap to a 2x2 grid with a top hairline on the
       second row. */
    .scoreboard {
      grid-template-columns: 1fr 1fr;
    }

    .score-cell:nth-child(3),
    .score-cell:nth-child(4) {
      border-top: 1px solid var(--em-line);
    }

    .score-cell:nth-child(3) {
      border-left: 0;
    }
  }

  @media (max-width: 560px) {
    .query-bar {
      flex-direction: column;
    }
  }
</style>
