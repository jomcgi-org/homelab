<script>
  // Shared input -> Run -> live latency -> result panel for all three
  // firecracker demo projects. Branches on project.key for the input
  // shape, the endpoint, and the result rendering, but shares one Run
  // button, one latency counter, and one TraceWaterfall. Styled to the
  // clean Grimoire palette (inherited --paper/--ink/etc custom properties
  // from the page wrapper), not the old brutalist heavy-border look.
  import TraceWaterfall from "./TraceWaterfall.svelte";

  let { project } = $props();

  // Inputs, seeded from the project's sample so Run works with zero edits,
  // but everything stays editable.
  let pythonCode = $state(project.sample.code ?? "");
  let semgrepPath = $state(project.sample.path ?? "");
  let semgrepCode = $state(project.sample.code ?? "");
  let gooseTask = $state(project.sample.task ?? "");
  let gooseRecipe = $state("");
  let gooseTier = $state("");

  // Run state
  let running = $state(false);
  let elapsedMs = $state(0);
  let finalMs = $state(null);
  let result = $state(null);
  let errorMsg = $state(null);
  let traceId = $state(null);
  let gooseStatus = $state(null);

  let wallTimer = null;
  let wallStart = 0;
  let goosePollHandle = null;

  function startTimer() {
    wallStart = performance.now();
    elapsedMs = 0;
    finalMs = null;
    wallTimer = setInterval(() => {
      elapsedMs = performance.now() - wallStart;
    }, 80);
  }

  function stopTimer() {
    if (wallTimer) {
      clearInterval(wallTimer);
      wallTimer = null;
    }
    finalMs = performance.now() - wallStart;
  }

  $effect(() => {
    return () => {
      if (wallTimer) clearInterval(wallTimer);
      if (goosePollHandle) clearTimeout(goosePollHandle);
    };
  });

  function resetRun() {
    result = null;
    errorMsg = null;
    traceId = null;
    gooseStatus = null;
    if (goosePollHandle) {
      clearTimeout(goosePollHandle);
      goosePollHandle = null;
    }
  }

  async function postJson(path, body) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data?.error || data?.detail || `HTTP ${res.status}`);
    }
    return data;
  }

  // Cap the poll so a stuck agent run does not spin forever. ~3 min at
  // 1.5s; after that we hand off to SigNoz rather than blocking the page.
  const MAX_GOOSE_POLLS = 120;

  function pollGoose(threadId) {
    return new Promise((resolve) => {
      let attempts = 0;
      const poll = async () => {
        attempts += 1;
        try {
          const res = await fetch(`/api/demos/firecracker/goose/${threadId}`);
          if (!res.ok) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data?.error || data?.detail || `HTTP ${res.status}`);
          }
          const data = await res.json();
          gooseStatus = data.status;
          if (data.done) {
            result = data;
            resolve();
            return;
          }
        } catch (e) {
          errorMsg = e?.message ?? String(e);
          resolve();
          return;
        }
        if (attempts >= MAX_GOOSE_POLLS) {
          gooseStatus = "still running, check the trace in SigNoz";
          resolve();
          return;
        }
        goosePollHandle = setTimeout(poll, 1500);
      };
      poll();
    });
  }

  async function run() {
    if (running) return;
    running = true;
    resetRun();
    startTimer();
    try {
      if (project.key === "python") {
        const data = await postJson("/api/demos/firecracker/python", {
          code: pythonCode,
        });
        result = data;
        traceId = data.trace_id ?? null;
        stopTimer();
      } else if (project.key === "semgrep") {
        const data = await postJson("/api/demos/firecracker/semgrep", {
          files: [{ path: semgrepPath || "snippet.py", content: semgrepCode }],
        });
        result = data;
        traceId = data.trace_id ?? null;
        stopTimer();
      } else if (project.key === "goose") {
        const data = await postJson("/api/demos/firecracker/goose", {
          task: gooseTask,
          ...(gooseRecipe.trim() ? { recipe: gooseRecipe.trim() } : {}),
          ...(gooseTier.trim() ? { tier: gooseTier.trim() } : {}),
        });
        traceId = data.trace_id ?? null;
        gooseStatus = "queued";
        await pollGoose(data.thread_id);
        stopTimer();
      }
    } catch (e) {
      errorMsg = e?.message ?? String(e);
      stopTimer();
    } finally {
      running = false;
    }
  }

  function severityClass(sev) {
    const s = (sev ?? "").toLowerCase();
    if (s === "error" || s === "high" || s === "critical") return "sev--high";
    if (s === "warning" || s === "warn" || s === "medium") return "sev--medium";
    return "sev--low";
  }

  let displayMs = $derived(finalMs ?? elapsedMs);
  let canRun = $derived.by(() => {
    if (project.key === "python") return pythonCode.trim().length > 0;
    if (project.key === "semgrep") return semgrepCode.trim().length > 0;
    if (project.key === "goose") return gooseTask.trim().length > 0;
    return false;
  });
</script>

<div class="run-panel">
  <div class="input-area">
    {#if project.key === "python"}
      <label class="field-label" for="python-code">code.py</label>
      <textarea
        id="python-code"
        class="code-input"
        bind:value={pythonCode}
        spellcheck="false"
        rows="12"
      ></textarea>
    {:else if project.key === "semgrep"}
      <label class="field-label" for="semgrep-path">file path</label>
      <input
        id="semgrep-path"
        class="text-input"
        bind:value={semgrepPath}
        spellcheck="false"
      />
      <label class="field-label" for="semgrep-code">file contents</label>
      <textarea
        id="semgrep-code"
        class="code-input"
        bind:value={semgrepCode}
        spellcheck="false"
        rows="12"
      ></textarea>
    {:else if project.key === "goose"}
      <label class="field-label" for="goose-task">task</label>
      <textarea
        id="goose-task"
        class="code-input code-input--prose"
        bind:value={gooseTask}
        spellcheck="true"
        rows="5"
      ></textarea>
      <div class="goose-extra">
        <div>
          <label class="field-label" for="goose-recipe">recipe (optional)</label>
          <input
            id="goose-recipe"
            class="text-input"
            bind:value={gooseRecipe}
            placeholder="default"
          />
        </div>
        <div>
          <label class="field-label" for="goose-tier">tier (optional)</label>
          <input
            id="goose-tier"
            class="text-input"
            bind:value={gooseTier}
            placeholder="default"
          />
        </div>
      </div>
    {/if}
  </div>

  <div class="run-bar">
    <button
      class="run-button"
      class:run-button--running={running}
      onclick={run}
      disabled={running || !canRun}
    >
      {#if running}
        <span class="spinner" aria-hidden="true"></span> Running
      {:else}
        Run
      {/if}
    </button>

    <div class="latency" aria-live="polite">
      <span class="latency-item">
        <span class="latency-label">wall</span>
        <span class="latency-value">{displayMs.toFixed(0)}ms</span>
      </span>
      {#if result?.overhead_ms != null}
        <!-- When the guest reports its own exec time we can split the two: the
             code's runtime vs the sandbox envelope around it. The sandbox
             number is the point of the demo, so it is emphasised. -->
        <span class="latency-item">
          <span class="latency-label">exec</span>
          <span class="latency-value">{result.duration_ms}ms</span>
        </span>
        <span
          class="latency-item latency-item--hero"
          title="Sandbox envelope: the whole invoke minus your code's exec (snapshot restore, vsock prime, readiness, teardown). Measured in-cluster, so the browser round-trip is excluded."
        >
          <span class="latency-label">sandbox</span>
          <span class="latency-value">{result.overhead_ms}ms</span>
        </span>
      {:else if result?.duration_ms != null}
        <span class="latency-item">
          <span class="latency-label">invocation</span>
          <span class="latency-value">{result.duration_ms}ms</span>
        </span>
      {/if}
    </div>
  </div>

  {#if errorMsg}
    <div class="error-banner" role="alert">{errorMsg}</div>
  {/if}

  {#if project.key === "goose" && running}
    <div class="goose-status">
      <span class="pulse-dot" aria-hidden="true"></span>
      status: {gooseStatus ?? "queued"}
    </div>
  {/if}

  {#if result}
    <div class="result-pane">
      {#if project.key === "python"}
        <div class="result-grid">
          <span class="result-key">exit code</span>
          <span
            class="result-val"
            class:result-val--bad={result.exit_code !== 0}
            >{result.exit_code}</span
          >
        </div>
        {#if result.stdout}
          <div class="result-block">
            <span class="body-label">stdout</span>
            <pre class="body-text">{result.stdout}</pre>
          </div>
        {/if}
        {#if result.stderr}
          <div class="result-block">
            <span class="body-label">stderr</span>
            <pre class="body-text body-text--error">{result.stderr}</pre>
          </div>
        {/if}
        {#if result.error}
          <div class="error-banner" role="alert">{result.error}</div>
        {/if}
      {:else if project.key === "semgrep"}
        {#if result.findings?.length}
          <ul class="findings">
            {#each result.findings as f}
              <li class="finding">
                <span class="finding-sev {severityClass(f.severity)}"
                  >{f.severity}</span
                >
                <span class="finding-loc">{f.path}:{f.line}{f.col ? `:${f.col}` : ""}</span>
                <span class="finding-rule">{f.rule_id}</span>
                <span class="finding-msg">{f.message}</span>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="result-empty">No findings.</p>
        {/if}
        {#if result.errors?.length}
          <div class="result-block">
            <span class="body-label">scan errors</span>
            <pre class="body-text body-text--error">{result.errors.join("\n")}</pre>
          </div>
        {/if}
        {#if result.error}
          <div class="error-banner" role="alert">{result.error}</div>
        {/if}
      {:else if project.key === "goose"}
        <div class="result-grid">
          <span class="result-key">status</span>
          <span class="result-val">{result.status}</span>
        </div>
        {#if result.result}
          <div class="result-block">
            <span class="body-label">result</span>
            <pre class="body-text">{typeof result.result === "string"
                ? result.result
                : JSON.stringify(result.result, null, 2)}</pre>
          </div>
        {/if}
        {#if result.result_error}
          <div class="error-banner" role="alert">{result.result_error}</div>
        {/if}
      {/if}
    </div>
  {/if}

  <TraceWaterfall {traceId} />
</div>

<style>
  .run-panel {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }

  .input-area {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .field-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-faint);
  }

  .code-input,
  .text-input {
    font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    font-size: 13px;
    color: var(--ink);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 6px;
    padding: 10px 12px;
    resize: vertical;
    line-height: 1.55;
  }

  .code-input:focus,
  .text-input:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent);
  }

  .goose-extra {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-top: 4px;
  }

  .run-bar {
    display: flex;
    align-items: center;
    gap: 20px;
    flex-wrap: wrap;
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
    transition:
      opacity 0.1s ease,
      transform 0.1s ease;
  }

  .run-button:hover:not(:disabled) {
    opacity: 0.9;
  }

  .run-button:disabled {
    opacity: 0.45;
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

  .latency {
    display: flex;
    gap: 20px;
  }

  .latency-item {
    display: flex;
    flex-direction: column;
    line-height: 1.2;
  }

  .latency-label {
    font-size: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--text-faint);
  }

  .latency-value {
    font-size: 15px;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
  }

  /* The sandbox overhead is the headline metric this demo exists to show, so
     tint its value with the accent and give its label a cursor hint for the
     explanatory tooltip. */
  .latency-item--hero {
    cursor: help;
  }

  .latency-item--hero .latency-value {
    color: var(--accent);
  }

  .goose-status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
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

  .error-banner {
    color: #fff; /* nosemgrep: svelte-hardcoded-color-in-style */
    background: var(--danger);
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 13px;
    font-weight: 500;
  }

  .result-pane {
    display: flex;
    flex-direction: column;
    gap: 12px;
    border: 1px solid var(--line);
    border-radius: 8px;
    background: var(--surface);
    padding: 16px 20px;
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

  .result-empty {
    font-size: 13px;
    color: var(--text-faint);
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
    background: color-mix(in srgb, #b8860b 12%, var(--surface));
    color: #8a6100; /* nosemgrep: svelte-hardcoded-color-in-style */
    border-color: color-mix(in srgb, #b8860b 30%, var(--line));
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
    .goose-extra {
      grid-template-columns: 1fr;
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
