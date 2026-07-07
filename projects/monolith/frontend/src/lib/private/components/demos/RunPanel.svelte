<script>
  // Shared input -> Run -> live latency -> result panel for all three
  // firecracker demo projects. Branches on project.key for the input
  // shape, the endpoint, and the result rendering, but shares one Run
  // button, one latency counter, and one TraceWaterfall.
  import TraceWaterfall from "./TraceWaterfall.svelte";

  let { project } = $props();

  // ── Inputs, seeded from the project's sample so Run works with zero
  // edits, but everything stays editable. ──────────────────────────────
  let pythonCode = $state(project.sample.code ?? "");
  let semgrepPath = $state(project.sample.path ?? "");
  let semgrepCode = $state(project.sample.code ?? "");
  let gooseTask = $state(project.sample.task ?? "");
  let gooseRecipe = $state("");
  let gooseTier = $state("");

  // ── Run state ──────────────────────────────────────────────────────
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

  // Cap the poll so a stuck agent run does not spin forever. ~3 min at 1.5s;
  // after that we hand off to SigNoz rather than blocking the modal.
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
        Run &#9654;
      {/if}
    </button>

    <div class="latency" aria-live="polite">
      <span class="latency-item">
        <span class="latency-label">wall time</span>
        <span class="latency-value">{displayMs.toFixed(0)}ms</span>
      </span>
      {#if result?.duration_ms != null}
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
    gap: 1rem;
  }

  .input-area {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .field-label {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
  }

  .code-input,
  .text-input {
    font-family: var(--font-mono);
    font-size: 0.8rem;
    color: var(--fg);
    background: var(--surface);
    border: 0.06rem solid var(--border);
    padding: 0.6rem 0.7rem;
    resize: vertical;
    line-height: 1.5;
  }

  .code-input--prose {
    resize: vertical;
  }

  .code-input:focus,
  .text-input:focus {
    outline: 2px solid var(--accent);
    outline-offset: -1px;
  }

  .goose-extra {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 0.25rem;
  }

  .run-bar {
    display: flex;
    align-items: center;
    gap: 1.25rem;
    flex-wrap: wrap;
  }

  .run-button {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--fg);
    background: var(--yellow);
    border: var(--border-heavy);
    padding: 0.55rem 1.25rem;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    transform: translate(0, 0);
    transition:
      transform 0.08s ease,
      box-shadow 0.08s ease,
      opacity 0.1s ease;
  }

  .run-button:hover:not(:disabled) {
    transform: translate(-2px, -2px);
    box-shadow: 4px 4px 0 0 var(--fg);
  }

  .run-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .run-button--running {
    background: var(--cream);
  }

  .spinner {
    width: 0.7rem;
    height: 0.7rem;
    border: 2px solid var(--fg);
    border-top-color: transparent;
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
    gap: 1.25rem;
  }

  .latency-item {
    display: flex;
    flex-direction: column;
    line-height: 1.2;
  }

  .latency-label {
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg-tertiary);
  }

  .latency-value {
    font-size: 1rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }

  .goose-status {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.75rem;
    color: var(--fg-tertiary);
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

  .error-banner {
    color: var(--fg);
    background: var(--coral);
    border: 2px solid var(--fg);
    padding: 0.6rem 0.8rem;
    font-size: 0.8rem;
    font-weight: 700;
  }

  .result-pane {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    border: var(--border-heavy);
    background: var(--bg);
    padding: 1rem 1.25rem;
  }

  .result-grid {
    display: flex;
    gap: 0.6rem;
    align-items: baseline;
    font-size: 0.8rem;
  }

  .result-key {
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    font-size: 0.65rem;
    color: var(--fg-tertiary);
  }

  .result-val {
    font-variant-numeric: tabular-nums;
    color: var(--fg);
  }

  .result-val--bad {
    color: var(--danger);
    font-weight: 700;
  }

  .result-block {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .body-label {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg-tertiary);
  }

  .body-text {
    font-family: var(--font-mono);
    font-size: 0.78rem;
    color: var(--fg);
    background: var(--surface);
    border: 0.04rem solid var(--border);
    padding: 0.6rem 0.7rem;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 14rem;
    overflow-y: auto;
  }

  .body-text--error {
    color: var(--danger);
  }

  .result-empty {
    font-size: 0.8rem;
    color: var(--fg-tertiary);
  }

  .findings {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    max-height: 16rem;
    overflow-y: auto;
  }

  .finding {
    display: grid;
    grid-template-columns: max-content max-content 1fr;
    gap: 0.4rem 0.6rem;
    align-items: baseline;
    font-size: 0.78rem;
    padding: 0.4rem 0;
    border-bottom: 0.04rem solid var(--border);
  }

  .finding-sev {
    font-size: 0.6rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    padding: 0.1rem 0.4rem;
    border: 1px solid var(--fg);
    grid-row: 1;
  }

  .sev--high {
    background: var(--coral);
  }

  .sev--medium {
    background: var(--yellow);
  }

  .sev--low {
    background: var(--surface);
  }

  .finding-loc {
    font-variant-numeric: tabular-nums;
    color: var(--fg-secondary);
    grid-row: 1;
  }

  .finding-rule {
    font-weight: 700;
    color: var(--fg-tertiary);
    grid-column: 3;
    grid-row: 1;
  }

  .finding-msg {
    grid-column: 1 / -1;
    color: var(--fg);
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
