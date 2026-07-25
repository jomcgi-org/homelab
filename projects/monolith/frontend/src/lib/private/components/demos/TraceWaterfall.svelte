<script>
  // Renders the real SigNoz trace for a firecracker invocation as a plain
  // CSS div waterfall (no chart lib): this is a private tool page and the
  // whole waterfall is a small amount of arithmetic, not worth an SSR
  // dependency.
  //
  // Props:
  //   traceId: string | null. When it changes, polling restarts cleanly.
  //
  // Polling: GET /api/demos/firecracker/trace/{traceId} every ~1000ms.
  // SigNoz ingests spans incrementally (they arrive over several seconds),
  // so the backend's `complete` flag only means "at least one span has
  // landed", not "fully ingested": treating it as terminal was the bug that
  // made the page get stuck "loading" or show a partial waterfall. Instead
  // we render every poll's spans immediately (progressive rendering) and
  // keep polling for a full 3-minute window so late spans keep streaming in
  // (a long async agent run emits its fc-invoke spans over minutes). We poll
  // every 1s while the span count is still changing, then back off to every
  // 3s once it looks stable, rather than stopping, so new spans still appear
  // without hammering the endpoint.
  let { traceId } = $props();

  // Poll fast while spans are still arriving, then back off to a slower
  // cadence once the count looks stable, but KEEP polling for the full
  // window: long async agent runs stream their fc-invoke spans in over
  // minutes, so stopping early (the old behavior) hid them.
  const POLL_MS_ACTIVE = 1000;
  const POLL_MS_IDLE = 3000;
  const STABLE_POLLS_BEFORE_IDLE = 3;
  const HARD_TIMEOUT_MS = 180_000;
  const MAX_CONSECUTIVE_ERRORS_WHEN_EMPTY = 3;

  // One fixed, distinct color per service so the trace's layers read as bands
  // (monolith request -> fc-invoke VM management -> guest agent) that stay
  // consistent across runs, instead of a hash that collides several services
  // onto the same blue. Colors are defined as CSS vars in theme.css; unknown
  // services fall back to a neutral grey.
  const SERVICE_COLORS = {
    "monolith-backend": "var(--svc-monolith)",
    "fc-invoke": "var(--svc-fc)",
    "goose-coding": "var(--svc-goose)",
    semgrep: "var(--svc-semgrep)",
  };

  function colorFor(service) {
    return SERVICE_COLORS[service] ?? "var(--svc-default)";
  }

  // Spans that run AFTER the response is already returned to the caller (VM
  // teardown / bundle cleanup): real work, but off the caller's critical path,
  // so we dim them and tag them rather than let them read as latency the caller
  // waited on.
  const OFF_PATH_SPANS = new Set([
    "guest_teardown",
    "vm_release",
    "bundle_cleanup",
  ]);

  function isOffPath(name) {
    return OFF_PATH_SPANS.has(name);
  }

  let spans = $state([]);
  // Goose's own internal spans (service `goose-coding`), correlated to this run
  // by the runner stamping `caller.trace_id` on them. Goose does not honor an
  // inbound TRACEPARENT, so these live in their own trace and cannot be truly
  // nested under the main waterfall: they render as their own sub-timeline.
  let correlated = $state([]);
  // "waiting" (no spans yet), "live" (spans present, still polling for
  // stability), "done" (stable or timed out), "error" (repeated failures
  // with zero spans ever seen).
  let status = $state("waiting");
  let fetchError = $state(null);

  let pollHandle = null;
  let pollStart = 0;
  let lastCount = -1;
  let stableStreak = 0;
  let consecutiveErrors = 0;

  function stopPolling() {
    if (pollHandle) clearTimeout(pollHandle);
    pollHandle = null;
  }

  function finish() {
    stopPolling();
    status = "done";
  }

  async function pollOnce(id) {
    let newSpans = null;
    let newCorrelated = null;
    try {
      const res = await fetch(`/api/demos/firecracker/trace/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      newSpans = Array.isArray(data.spans) ? data.spans : [];
      newCorrelated = Array.isArray(data.correlated) ? data.correlated : [];
      fetchError = null;
      consecutiveErrors = 0;
    } catch (e) {
      consecutiveErrors += 1;
      if (
        spans.length === 0 &&
        consecutiveErrors >= MAX_CONSECUTIVE_ERRORS_WHEN_EMPTY
      ) {
        fetchError = e?.message ?? String(e);
        status = "error";
        stopPolling();
        return;
      }
      // Keep the last-known spans on a flaky poll, just try again.
    }

    if (traceId !== id) return; // traceId changed mid-flight, effect already restarted

    if (newCorrelated !== null) {
      correlated = newCorrelated;
    }

    if (newSpans !== null) {
      spans = newSpans;
      if (spans.length > 0) {
        status = "live";
        if (spans.length === lastCount) {
          stableStreak += 1;
        } else {
          stableStreak = 1;
        }
        lastCount = spans.length;
      }
    }

    if (performance.now() - pollStart >= HARD_TIMEOUT_MS) {
      finish();
      return;
    }
    // Keep polling for the whole window so late spans (long agent runs) stream
    // in; once the count looks stable, slow the cadence rather than stopping.
    const interval =
      stableStreak >= STABLE_POLLS_BEFORE_IDLE ? POLL_MS_IDLE : POLL_MS_ACTIVE;
    pollHandle = setTimeout(() => pollOnce(id), interval);
  }

  $effect(() => {
    stopPolling();
    spans = [];
    correlated = [];
    status = "waiting";
    fetchError = null;
    lastCount = -1;
    stableStreak = 0;
    consecutiveErrors = 0;
    if (!traceId) {
      return;
    }
    pollStart = performance.now();
    pollOnce(traceId);
    return () => stopPolling();
  });

  // Span depth by walking the parent chain, for a light indent that reads
  // as a call tree without needing a real tree layout. A span whose parent
  // isn't in the given span set is treated as a root (depth 0). Scoped to the
  // passed set so the main waterfall and the correlated goose set each get
  // their own depth map.
  function depthMapFor(set) {
    const byId = new Map(set.map((s) => [s.span_id, s]));
    const depth = new Map();
    function depthOf(id, guard = 0) {
      if (depth.has(id)) return depth.get(id);
      const span = byId.get(id);
      if (!span || !span.parent_span_id || guard > 32) {
        depth.set(id, 0);
        return 0;
      }
      const d = byId.has(span.parent_span_id)
        ? depthOf(span.parent_span_id, guard + 1) + 1
        : 0;
      depth.set(id, d);
      return d;
    }
    for (const s of set) depthOf(s.span_id);
    return depth;
  }

  function timelineEndFor(set) {
    return Math.max(
      1,
      ...set.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0)),
    );
  }

  // Bar geometry against a set's OWN timelineEnd, so the correlated goose set
  // renders as its own self-contained sub-timeline (its earliest span at 0).
  function barStyleFor(span, timelineEnd) {
    const left = (Math.max(0, span.start_ms ?? 0) / timelineEnd) * 100;
    const width = Math.max(0.6, ((span.duration_ms ?? 0) / timelineEnd) * 100);
    return `left: ${left}%; width: ${width}%;`;
  }

  let depthById = $derived(depthMapFor(spans));
  let sortedSpans = $derived(
    [...spans].sort((a, b) => a.start_ms - b.start_ms),
  );
  let timelineEnd = $derived(timelineEndFor(spans));

  let correlatedDepthById = $derived(depthMapFor(correlated));
  let sortedCorrelated = $derived(
    [...correlated].sort((a, b) => a.start_ms - b.start_ms),
  );
  let correlatedTimelineEnd = $derived(timelineEndFor(correlated));

  let axisTicks = $derived.by(() => {
    const end = timelineEnd;
    return [0, 0.25, 0.5, 0.75, 1].map((f) => ({
      pct: f * 100,
      label: `${Math.round(f * end)}ms`,
    }));
  });

  // "setup" is the daemon-side pre-exec latency: from the request landing in the
  // fc-invoke daemon (its earliest span) to the workload starting to execute
  // (guest_exec.start). That window is the sandbox envelope BEFORE the code runs:
  // slot wait + snapshot restore + vsock prime + readiness. It is the number this
  // demo exists to show, so we derive it from the same spans as the waterfall.
  // Keyed only on the guest_exec span and its (daemon) service, so it is robust
  // to the warm/cold paths and to phase-span renames. null until guest_exec has
  // ingested.
  let setupMs = $derived.by(() => {
    const exec = spans.find((s) => s.name === "guest_exec");
    if (!exec) return null;
    const daemonStarts = spans
      .filter((s) => s.service === exec.service)
      .map((s) => s.start_ms ?? 0);
    if (daemonStarts.length === 0) return null;
    return Math.max(0, exec.start_ms - Math.min(...daemonStarts));
  });
</script>

<div class="waterfall">
  <div class="waterfall-header">
    <h3 class="waterfall-title">Trace waterfall</h3>
    {#if traceId}
      <a
        class="signoz-link"
        href={`https://private.jomcgi.dev/app/signoz/trace/${traceId}`}
        target="_blank"
        rel="noopener noreferrer"
      >
        View in SigNoz
      </a>
    {/if}
  </div>

  {#if setupMs != null}
    <!-- Headline metric: the daemon's pre-exec setup, i.e. how long from the
         request landing in fc-invoke to the workload actually executing. This is
         the cost of the sandbox before any user code runs. -->
    <div
      class="setup-stat"
      title="From the request landing in the fc-invoke daemon to the workload starting to execute: slot wait + snapshot restore + vsock prime + readiness. The sandbox's cost before your code runs."
    >
      <span class="setup-label">setup · landing → exec</span>
      <span class="setup-value">{setupMs.toFixed(1)}ms</span>
    </div>
  {/if}

  {#if !traceId}
    <p class="waterfall-empty">Run something to see its trace here.</p>
  {:else if status === "error"}
    <p class="waterfall-empty waterfall-empty--error">
      Couldn't load the trace: {fetchError}
    </p>
  {:else if spans.length === 0 && status === "done"}
    <p class="waterfall-empty">
      No spans landed for this trace within 60s. It may not have been sampled,
      or ingestion is lagging. Try "View in SigNoz" above.
    </p>
  {:else if spans.length === 0}
    <div class="waterfall-waiting">
      <span class="pulse-dot" aria-hidden="true"></span>
      <span>waiting for trace to ingest...</span>
    </div>
  {:else}
    <div class="axis" aria-hidden="true">
      {#each axisTicks as tick}
        <span class="axis-tick" style={`left: ${tick.pct}%;`}>{tick.label}</span
        >
      {/each}
    </div>

    <div class="rows">
      {#each sortedSpans as span (span.span_id)}
        <div
          class="row"
          class:row--offpath={isOffPath(span.name)}
          style={`padding-left: ${(depthById.get(span.span_id) ?? 0) * 0.9}rem;`}
        >
          <span class="row-label" title={`${span.name} · ${span.service}`}>
            {span.name}
            {#if isOffPath(span.name)}<span class="row-tag"
                >off critical path</span
              >{/if}
            <span class="row-service">{span.service}</span>
          </span>
          <div class="row-track">
            <div
              class="row-bar"
              class:row-bar--error={span.error}
              style={`${barStyleFor(span, timelineEnd)} background: ${span.error ? "var(--danger)" : colorFor(span.service)};`}
            >
              <span class="row-duration">{span.duration_ms}ms</span>
            </div>
          </div>
        </div>
      {/each}
    </div>

    {#if status === "live"}
      <div class="waterfall-footer">
        <span class="pulse-dot" aria-hidden="true"></span>
        updating, showing {spans.length} span{spans.length === 1 ? "" : "s"} so far
      </div>
    {/if}
  {/if}

  {#if correlated.length > 0}
    <div class="correlated">
      <div class="correlated-header">
        <h4 class="correlated-title">Agent internals (goose)</h4>
        <span class="correlated-badge">correlated</span>
      </div>
      <p class="correlated-note">
        The agent's own spans, correlated to this run by trace id. Goose runs in
        its own trace (it does not honor an inbound parent context), so these
        are shown on their own relative timeline rather than nested above.
      </p>
      <div class="rows">
        {#each sortedCorrelated as span (span.span_id)}
          <div
            class="row"
            style={`padding-left: ${(correlatedDepthById.get(span.span_id) ?? 0) * 0.9}rem;`}
          >
            <span class="row-label" title={`${span.name} · ${span.service}`}>
              {span.name}
              <span class="row-service">{span.service}</span>
            </span>
            <div class="row-track">
              <div
                class="row-bar"
                class:row-bar--error={span.error}
                style={`${barStyleFor(span, correlatedTimelineEnd)} background: ${span.error ? "var(--danger)" : "var(--svc-goose)"};`}
              >
                <span class="row-duration">{span.duration_ms}ms</span>
              </div>
            </div>
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .waterfall {
    display: flex;
    flex-direction: column;
    gap: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface);
    padding: 16px 20px;
  }

  .waterfall-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 10px;
  }

  .waterfall-title {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--ink);
    margin: 0;
  }

  .signoz-link {
    font-size: 12px;
    font-weight: 600;
    color: var(--accent);
    text-decoration: none;
  }

  .signoz-link:hover {
    text-decoration: underline;
  }

  /* The setup metric is the headline number this trace exists to expose, so it
     gets a tinted pill with the value in the accent color and a help cursor for
     the explanatory tooltip. */
  .setup-stat {
    display: inline-flex;
    align-items: baseline;
    gap: 8px;
    align-self: flex-start;
    padding: 5px 10px;
    border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--line));
    background: color-mix(in srgb, var(--accent) 8%, var(--surface));
    border-radius: 6px;
    cursor: help;
  }

  .setup-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-faint);
  }

  .setup-value {
    font-size: 15px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--accent);
  }

  .waterfall-empty {
    font-size: 13px;
    color: var(--text-faint);
    padding: 8px 0;
    margin: 0;
  }

  .waterfall-empty--error {
    color: var(--danger);
  }

  .waterfall-waiting,
  .waterfall-footer {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-faint);
    padding: 5px 0;
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

  .axis {
    position: relative;
    height: 16px;
    margin-left: 9rem;
    border-bottom: 1px solid var(--line);
  }

  .axis-tick {
    position: absolute;
    transform: translateX(-50%);
    font-size: 10.5px;
    color: var(--text-faint);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .rows {
    display: flex;
    flex-direction: column;
    gap: 6px;
    max-height: 16rem;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--text-faint) transparent;
  }

  .row {
    display: grid;
    grid-template-columns: 9rem 1fr;
    align-items: center;
    gap: 10px;
  }

  .row-label {
    font-size: 11.5px;
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    line-height: 1.2;
  }

  .row-service {
    font-size: 10px;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  /* Off-critical-path spans (teardown/cleanup after the response): dimmed bar
     plus a small tag so they do not read as latency the caller waited on. */
  .row--offpath .row-bar {
    opacity: 0.4;
  }

  .row-tag {
    font-size: 9px;
    color: var(--text-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    border: 1px solid var(--line);
    border-radius: 3px;
    padding: 0 4px;
    margin-left: 4px;
    white-space: nowrap;
  }

  .row-track {
    position: relative;
    height: 18px;
    background: var(--paper);
    border-radius: 3px;
  }

  .row-bar {
    position: absolute;
    top: 0;
    height: 100%;
    min-width: 2px;
    display: flex;
    align-items: center;
    padding: 0 5px;
    box-sizing: border-box;
    border-radius: 3px;
  }

  .row-duration {
    font-size: 10px;
    font-weight: 600;
    color: #fff; /* nosemgrep: svelte-hardcoded-color-in-style */
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    text-shadow: 0 0 2px rgba(0, 0, 0, 0.35);
  }

  .correlated {
    display: flex;
    flex-direction: column;
    gap: 10px;
    border-top: 1px dashed var(--line);
    padding-top: 12px;
  }

  .correlated-header {
    display: flex;
    align-items: center;
    gap: 10px;
  }

  .correlated-title {
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--ink);
    margin: 0;
  }

  .correlated-badge {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--svc-goose);
    border: 1px solid var(--svc-goose);
    border-radius: 999px;
    padding: 1px 7px;
  }

  .correlated-note {
    font-size: 11.5px;
    color: var(--text-faint);
    line-height: 1.4;
    margin: 0;
  }

  @media (prefers-reduced-motion: reduce) {
    .pulse-dot {
      animation: none;
      opacity: 0.8;
    }
  }
</style>
