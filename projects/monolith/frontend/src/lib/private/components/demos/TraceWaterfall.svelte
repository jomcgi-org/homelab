<script>
  // Renders the real SigNoz trace for a firecracker invocation as a plain
  // CSS/SVG-free waterfall (div bars on a shared time axis). No chart lib:
  // this is a private tool page and the whole waterfall is ~20 lines of
  // arithmetic, not worth an SSR dependency.
  //
  // Props:
  //   traceId: string | null. When it changes, polling restarts.
  //
  // Polls GET /api/demos/firecracker/trace/{traceId} every 1.5s for up to
  // ~10s (7 attempts) until `complete` is true. Ingestion into SigNoz lags
  // 5-10s behind the invocation, so an empty/incomplete response right
  // after Run is expected, not an error.
  let { traceId } = $props();

  const POLL_MS = 1500;
  const MAX_ATTEMPTS = 7;

  // Stable-ish palette keyed by service name via a small string hash, so
  // the same service always lands on the same swatch across runs.
  const PALETTE = ["var(--accent)", "var(--green)", "var(--yellow)", "var(--grey)"];

  function colorFor(service) {
    if (!service) return "var(--grey)";
    let h = 0;
    for (let i = 0; i < service.length; i++) {
      h = (h * 31 + service.charCodeAt(i)) | 0;
    }
    return PALETTE[Math.abs(h) % PALETTE.length];
  }

  let spans = $state([]);
  let complete = $state(false);
  let attempts = $state(0);
  let loading = $state(false);
  let fetchError = $state(null);
  let pollHandle = null;

  function stopPolling() {
    if (pollHandle) clearTimeout(pollHandle);
    pollHandle = null;
  }

  async function pollOnce(id) {
    attempts += 1;
    try {
      const res = await fetch(`/api/demos/firecracker/trace/${id}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      spans = Array.isArray(data.spans) ? data.spans : [];
      complete = Boolean(data.complete);
      fetchError = null;
    } catch (e) {
      // A single flaky poll shouldn't kill the whole waterfall, keep
      // retrying until MAX_ATTEMPTS, only surface the error if we never
      // got a usable response.
      if (spans.length === 0) fetchError = e?.message ?? String(e);
    }
    if (!complete && attempts < MAX_ATTEMPTS && traceId === id) {
      pollHandle = setTimeout(() => pollOnce(id), POLL_MS);
    } else {
      loading = false;
    }
  }

  $effect(() => {
    stopPolling();
    spans = [];
    complete = false;
    attempts = 0;
    fetchError = null;
    if (!traceId) {
      loading = false;
      return;
    }
    loading = true;
    pollOnce(traceId);
    return () => stopPolling();
  });

  // Span depth by walking the parent chain, for a light indent that reads
  // as a call tree without needing a real tree layout.
  let depthById = $derived.by(() => {
    const byId = new Map(spans.map((s) => [s.span_id, s]));
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
    for (const s of spans) depthOf(s.span_id);
    return depth;
  });

  let sortedSpans = $derived(
    [...spans].sort((a, b) => a.start_ms - b.start_ms),
  );

  let timelineEnd = $derived(
    Math.max(1, ...spans.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0))),
  );

  let axisTicks = $derived.by(() => {
    const end = timelineEnd;
    return [0, 0.25, 0.5, 0.75, 1].map((f) => ({
      pct: f * 100,
      label: `${Math.round(f * end)}ms`,
    }));
  });

  function barStyle(span) {
    const left = (Math.max(0, span.start_ms ?? 0) / timelineEnd) * 100;
    const width = Math.max(
      0.6,
      ((span.duration_ms ?? 0) / timelineEnd) * 100,
    );
    return `left: ${left}%; width: ${width}%;`;
  }
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
        View in SigNoz &#8599;
      </a>
    {/if}
  </div>

  {#if !traceId}
    <p class="waterfall-empty">Run something to see its trace here.</p>
  {:else if fetchError && spans.length === 0}
    <p class="waterfall-empty waterfall-empty--error">
      Couldn't load the trace: {fetchError}
    </p>
  {:else if spans.length === 0}
    <div class="waterfall-waiting">
      <span class="pulse-dot" aria-hidden="true"></span>
      <span>
        {complete ? "trace has no spans yet" : "waiting for trace to ingest..."}
      </span>
    </div>
  {:else}
    <div class="axis" aria-hidden="true">
      {#each axisTicks as tick}
        <span class="axis-tick" style={`left: ${tick.pct}%;`}>{tick.label}</span>
      {/each}
    </div>

    <div class="rows">
      {#each sortedSpans as span (span.span_id)}
        <div class="row" style={`padding-left: ${(depthById.get(span.span_id) ?? 0) * 0.9}rem;`}>
          <span class="row-label" title={`${span.name} · ${span.service}`}>
            {span.name}
            <span class="row-service">{span.service}</span>
          </span>
          <div class="row-track">
            <div
              class="row-bar"
              class:row-bar--error={span.error}
              style={`${barStyle(span)} background: ${span.error ? "var(--coral)" : colorFor(span.service)};`}
            >
              <span class="row-duration">{span.duration_ms}ms</span>
            </div>
          </div>
        </div>
      {/each}
    </div>

    {#if !complete}
      <div class="waterfall-footer">
        <span class="pulse-dot" aria-hidden="true"></span>
        still ingesting, showing {spans.length} span{spans.length === 1 ? "" : "s"} so far
      </div>
    {/if}
  {/if}
</div>

<style>
  .waterfall {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    border: var(--border-heavy);
    background: var(--bg);
    padding: 1rem 1.25rem;
  }

  .waterfall-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 0.06rem solid var(--border);
    padding-bottom: 0.6rem;
  }

  .waterfall-title {
    font-family: var(--font-mono);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    margin: 0;
  }

  .signoz-link {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 700;
    color: var(--accent);
    letter-spacing: 0.04em;
  }

  .signoz-link:hover {
    text-decoration: underline;
  }

  .waterfall-empty {
    font-size: 0.8rem;
    color: var(--fg-tertiary);
    padding: 0.5rem 0;
  }

  .waterfall-empty--error {
    color: var(--danger);
  }

  .waterfall-waiting,
  .waterfall-footer {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    padding: 0.35rem 0;
  }

  .pulse-dot {
    width: 0.5rem;
    height: 0.5rem;
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
    height: 1rem;
    margin-left: 9rem;
    border-bottom: 0.04rem solid var(--border);
  }

  .axis-tick {
    position: absolute;
    transform: translateX(-50%);
    font-size: 0.65rem;
    color: var(--fg-tertiary);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .rows {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    max-height: 16rem;
    overflow-y: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--fg-tertiary) transparent;
  }

  .row {
    display: grid;
    grid-template-columns: 9rem 1fr;
    align-items: center;
    gap: 0.6rem;
  }

  .row-label {
    font-size: 0.7rem;
    color: var(--fg);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    display: flex;
    flex-direction: column;
    line-height: 1.2;
  }

  .row-service {
    font-size: 0.6rem;
    color: var(--fg-tertiary);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  .row-track {
    position: relative;
    height: 1.15rem;
    background: var(--surface);
  }

  .row-bar {
    position: absolute;
    top: 0;
    height: 100%;
    min-width: 2px;
    display: flex;
    align-items: center;
    padding: 0 0.3rem;
    box-sizing: border-box;
    border: 1px solid var(--fg);
  }

  .row-bar--error {
    border-color: var(--fg);
  }

  .row-duration {
    font-size: 0.6rem;
    font-weight: 700;
    color: var(--fg);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
    text-shadow: 0 0 2px var(--bg);
  }

  @media (prefers-reduced-motion: reduce) {
    .pulse-dot {
      animation: none;
      opacity: 0.8;
    }
  }
</style>
