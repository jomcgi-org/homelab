<script>
  import { onMount } from "svelte";
  // This demo deliberately uses the public blog tokens because it is presented
  // beside the post, not as part of the private dashboard design system.
  import "$lib/public/styles/technical-drawing.css";
  import {
    calculateTurnMetrics,
    deriveModelState,
    formatBytes,
    formatRate,
    trackPeaks,
  } from "./metrics.js";

  const CHAT_URL = "/private/demos/qwen-flash/chat";
  const STATS_URL = "/private/demos/qwen-flash/stats";
  const EMPTY_TURN_METRICS = {
    ttftMs: null,
    tokensPerSecond: 0,
    timeToFirstReasoningMs: null,
    timeToFirstAnswerMs: null,
    reasoningTokens: 0,
    answerTokens: 0,
  };

  let messages = $state([]);
  let inputText = $state("");
  let isInFlight = $state(false);
  let firstTokenSeen = $state(false);
  // Drives GENERATION, not just display: the server reads enableThinking and
  // passes it to the model, so flipping this mid-demo shows the contrast
  // between a reasoned answer and a direct one.
  let thinkingEnabled = $state(true);
  let stats = $state(null);
  let statsError = $state("");
  let peaks = $state({ decodeTps: 0, prefillTps: 0 });
  let turnStartedAt = 0;
  let turnChunks = [];
  let turnMetrics = $state({ ...EMPTY_TURN_METRICS });
  let controller;
  let chatLog;
  let statsTimer;
  let pollGeneration = 0;
  let mounted = false;

  let modelState = $derived(deriveModelState(isInFlight, firstTokenSeen));

  function formatCount(value) {
    return Math.max(0, Number(value) || 0).toLocaleString("en-US");
  }

  function formatUptime(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remaining = total % 60;
    return `${String(hours).padStart(3, "0")}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
  }

  function formatDuration(milliseconds) {
    return milliseconds === null
      ? "--.- s"
      : `${(milliseconds / 1000).toFixed(1)} s`;
  }

  function formatReasoningRatio(reasoningTokens, answerTokens) {
    if (!reasoningTokens && !answerTokens) return "-- / --";
    if (reasoningTokens < 10 || answerTokens < 10) {
      return `${reasoningTokens} / ${answerTokens}`;
    }
    return `${(reasoningTokens / answerTokens).toFixed(1)}x`;
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
    });
  }

  async function pollStats() {
    try {
      const response = await fetch(STATS_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = await response.json();
      stats = next;
      peaks = trackPeaks(peaks, next);
      statsError = "";
    } catch (error) {
      statsError = `Live stats unavailable (${error.message})`;
    }
  }

  function scheduleStats(delay) {
    if (!mounted) return;
    clearTimeout(statsTimer);
    const generation = ++pollGeneration;
    statsTimer = setTimeout(async () => {
      await pollStats();
      if (mounted && generation === pollGeneration) {
        scheduleStats(isInFlight ? 1000 : 5000);
      }
    }, delay);
  }

  function appendDelta(delta) {
    const now = performance.now();
    const assistant = messages[messages.length - 1];
    const reasoningContent =
      typeof delta.reasoning_content === "string"
        ? delta.reasoning_content
        : "";
    const content = typeof delta.content === "string" ? delta.content : "";

    if (delta.role === "assistant") assistant.role = delta.role;
    if (reasoningContent) assistant.reasoningContent += reasoningContent;
    if (content) assistant.content += content;

    turnChunks = [
      ...turnChunks,
      {
        at: now,
        role: delta.role,
        reasoning_content: reasoningContent,
        content,
      },
    ];
    turnMetrics = calculateTurnMetrics(turnStartedAt, turnChunks);
    firstTokenSeen = turnMetrics.ttftMs !== null;
    scrollToBottom();
  }

  function consumeEvent(block) {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data || data === "[DONE]") return;

    try {
      const event = JSON.parse(data);
      const delta = event.choices?.[0]?.delta;
      if (delta && typeof delta === "object") appendDelta(delta);
    } catch {
      // A malformed upstream event is ignored without interrupting the stream.
    }
  }

  async function sendMessage() {
    const text = inputText.trim();
    if (!text || isInFlight) return;

    inputText = "";
    messages.push({ role: "user", content: text, reasoningContent: "" });
    messages.push({ role: "assistant", content: "", reasoningContent: "" });
    isInFlight = true;
    firstTokenSeen = false;
    turnStartedAt = performance.now();
    turnChunks = [];
    turnMetrics = { ...EMPTY_TURN_METRICS };
    controller = new AbortController();
    scheduleStats(0);
    scrollToBottom();

    try {
      const response = await fetch(CHAT_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: messages.slice(0, -1).map(({ role, content }) => ({
            role,
            content,
          })),
          enableThinking: thinkingEnabled,
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const detail = await response.text();
        throw new Error(detail || `HTTP ${response.status}`);
      }
      if (!response.body) throw new Error("The response stream was empty");

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const blocks = buffer.split(/\r?\n\r?\n/);
        buffer = blocks.pop() ?? "";
        blocks.forEach(consumeEvent);
      }
      buffer += decoder.decode();
      if (buffer.trim()) consumeEvent(buffer);
    } catch (error) {
      const assistant = messages[messages.length - 1];
      if (error.name === "AbortError") {
        if (!assistant.content) assistant.content = "Generation stopped.";
      } else {
        assistant.content += `${assistant.content ? "\n\n" : ""}Connection error: ${error.message}`;
      }
    } finally {
      isInFlight = false;
      controller = undefined;
      scheduleStats(0);
      scrollToBottom();
    }
  }

  function stopGeneration() {
    controller?.abort();
  }

  function onInputKeydown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage();
    }
  }

  onMount(() => {
    mounted = true;
    scheduleStats(0);
    return () => {
      mounted = false;
      clearTimeout(statsTimer);
      controller?.abort();
    };
  });
</script>

<svelte:head>
  <title>Qwen Flash Demo · private.jomcgi.dev</title>
  <meta
    name="description"
    content="A 125B parameter mixture-of-experts model served from one RTX 4090."
  />
</svelte:head>

<main class="td demo-page">
  <header class="page-header">
    <div>
      <p class="kicker">/ Live inference experiment</p>
      <h1>125B parameters.<br /><span>One RTX 4090.</span></h1>
    </div>
    <p class="lede">
      Qwen3.8-Flash-Next pages mixture-of-experts weights from NVMe while a
      single 24 GB card handles the active working set.
    </p>
  </header>

  <div class="demo-grid">
    <section class="chat-card" aria-label="Chat with Qwen3.8-Flash-Next">
      <header class="card-heading">
        <div>
          <p class="section-label">01 / Chat</p>
          <h2>Ask the 125B model</h2>
        </div>
        <div class="heading-controls">
          <label class="thinking-toggle">
            <input type="checkbox" bind:checked={thinkingEnabled} />
            <span>Thinking</span>
          </label>
          <div class="state" data-state={modelState} aria-live="polite">
            <span class="state-dot"></span>
            {modelState}
          </div>
        </div>
      </header>

      <div class="turn-strip">
        <div>
          <span>Time to first token</span>
          <strong>{formatDuration(turnMetrics.ttftMs)}</strong>
        </div>
        <div>
          <span>This turn</span>
          <strong>{formatRate(turnMetrics.tokensPerSecond)} tok/s</strong>
        </div>
        <div>
          <span>First reasoning</span>
          <strong>{formatDuration(turnMetrics.timeToFirstReasoningMs)}</strong>
        </div>
        <div>
          <span>First answer</span>
          <strong>{formatDuration(turnMetrics.timeToFirstAnswerMs)}</strong>
        </div>
        <div>
          <span>Reasoning / answer</span>
          <strong>
            {formatReasoningRatio(
              turnMetrics.reasoningTokens,
              turnMetrics.answerTokens,
            )}
          </strong>
        </div>
      </div>

      <div class="messages" bind:this={chatLog} aria-live="polite">
        {#if messages.length === 0}
          <div class="empty-state">
            <p class="empty-index">125B / 24GB</p>
            <h3>The model is larger than VRAM.</h3>
            <p>
              Ask a question and watch the live panel while the prompt is
              prefilling and the experts are paged from NVMe.
            </p>
          </div>
        {/if}

        {#each messages as message, index}
          <article
            class:assistant={message.role === "assistant"}
            class="message"
          >
            <p class="message-role">{message.role}</p>
            {#if message.role === "assistant" && !message.content && !message.reasoningContent && isInFlight && index === messages.length - 1}
              <p class="waiting">
                <span class="waiting-mark"></span>
                Waiting for first token. Cold expert pages can take tens of seconds
                to reach the GPU.
              </p>
            {:else if message.role === "assistant"}
              {#if message.reasoningContent}
                <details
                  class="thinking-region"
                  open={isInFlight && index === messages.length - 1}
                >
                  <summary>Thinking</summary>
                  <pre>{message.reasoningContent}</pre>
                </details>
              {/if}
              <pre class="answer-region">{message.content}</pre>
            {:else}
              <pre>{message.content}</pre>
            {/if}
          </article>
        {/each}
      </div>

      <form
        class="composer"
        onsubmit={(event) => {
          event.preventDefault();
          void sendMessage();
        }}
      >
        <label for="qwen-prompt">Message</label>
        <textarea
          id="qwen-prompt"
          bind:value={inputText}
          onkeydown={onInputKeydown}
          placeholder="Ask something worth 125 billion parameters..."
          rows="3"
          disabled={isInFlight}></textarea>
        <div class="composer-actions">
          <p>Enter to send · Shift+Enter for a new line</p>
          {#if isInFlight}
            <button class="stop-button" type="button" onclick={stopGeneration}>
              Stop generation
            </button>
          {:else}
            <button
              class="send-button"
              type="submit"
              disabled={!inputText.trim()}
            >
              Send
            </button>
          {/if}
        </div>
      </form>
    </section>

    <aside class="metrics-card" aria-label="Live model and hardware metrics">
      <header class="hardware-header">
        <div class="hardware-topline">
          <p class="section-label">02 / Hardware</p>
          <span class="live-tag">Live</span>
        </div>
        <p class="setup-callout">
          A 125B-parameter model fits on one 24 GB card only because its weights
          live on NVMe. The server pages experts on demand.
        </p>
        <p class="gpu-name">{stats?.gpus?.[0]?.name ?? "GPU stats loading"}</p>
        <div class="vram-hero">
          <strong>{stats ? formatBytes(stats.vram_bytes) : "--.- GB"}</strong>
          <span>VRAM in use</span>
        </div>
        <p class="vram-total">
          of {stats ? formatBytes(stats.gpus?.[0]?.total_bytes) : "--.- GB"}
          available
        </p>
        <dl class="hardware-facts">
          <div>
            <dt>Presented model</dt>
            <dd>Qwen3.8-Flash-Next</dd>
          </div>
          <div>
            <dt>Parameters</dt>
            <dd>125B</dd>
          </div>
          <div>
            <dt>Server model ID</dt>
            <dd>{stats?.model?.id ?? "qwen3.6-27b"}</dd>
          </div>
          <div>
            <dt>Context</dt>
            <dd>{formatCount(stats?.model?.ctx)} tokens</dd>
          </div>
          <div>
            <dt>Architecture</dt>
            <dd>{stats?.model?.moe ? "Mixture of experts" : "Loading"}</dd>
          </div>
          <div>
            <dt>Expert storage</dt>
            <dd>NVMe paging</dd>
          </div>
        </dl>
      </header>

      {#if statsError}
        <p class="stats-error" role="status">{statsError}</p>
      {/if}

      <section class="metric-group current-group">
        <h3><span>Current</span> Right now</h3>
        <dl class="metric-grid">
          <div>
            <dt>Decode</dt>
            <dd>
              {formatRate(stats?.throughput?.decode_tps)} <small>tok/s</small>
            </dd>
          </div>
          <div>
            <dt>Prefill</dt>
            <dd>
              {formatRate(stats?.throughput?.prefill_tps)} <small>tok/s</small>
            </dd>
          </div>
          <div>
            <dt>Active requests</dt>
            <dd>{formatCount(stats?.requests?.active)}</dd>
          </div>
          <div>
            <dt>KV pages</dt>
            <dd>
              {formatCount(stats?.kv?.used_pages)}
              <small>/ {formatCount(stats?.kv?.total_pages)}</small>
            </dd>
          </div>
        </dl>
      </section>

      <section class="metric-group">
        <h3><span>Peak</span> Since page load</h3>
        <dl class="metric-grid two-up">
          <div>
            <dt>Decode</dt>
            <dd>{formatRate(peaks.decodeTps)} <small>tok/s</small></dd>
          </div>
          <div>
            <dt>Prefill</dt>
            <dd>{formatRate(peaks.prefillTps)} <small>tok/s</small></dd>
          </div>
        </dl>
      </section>

      <section class="metric-group">
        <h3><span>Total</span> Server lifetime</h3>
        <dl class="metric-grid">
          <div>
            <dt>Completed</dt>
            <dd>{formatCount(stats?.requests?.completed)}</dd>
          </div>
          <div>
            <dt>Prompt tokens</dt>
            <dd>{formatCount(stats?.requests?.prompt_tokens_total)}</dd>
          </div>
          <div>
            <dt>Completion tokens</dt>
            <dd>{formatCount(stats?.requests?.completion_tokens_total)}</dd>
          </div>
          <div>
            <dt>Uptime</dt>
            <dd>{formatUptime(stats?.uptime_s)}</dd>
          </div>
        </dl>
      </section>

      <p class="panel-note">
        Current values come from the server. Peak values are retained in this
        browser. Turn timing starts when you press Send.
      </p>
    </aside>
  </div>
</main>

<style>
  .demo-page {
    min-height: 100vh;
    padding: clamp(1.25rem, 3vw, 3rem);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
  }

  .page-header {
    display: grid;
    grid-template-columns: minmax(0, 1.2fr) minmax(18rem, 0.8fr);
    align-items: end;
    gap: 2rem;
    max-width: 90rem;
    margin: 0 auto clamp(1.5rem, 4vw, 3.5rem);
    padding-bottom: 1.5rem;
    border-bottom: 1px solid var(--stroke);
  }

  .kicker,
  .section-label,
  .message-role,
  .live-tag,
  .state,
  .composer label {
    margin: 0;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.13em;
    text-transform: uppercase;
  }

  .page-header h1 {
    max-width: 12ch;
    margin: 0.55rem 0 0;
    font-size: clamp(2.7rem, 6vw, 6.8rem);
    font-weight: 500;
    letter-spacing: -0.065em;
    line-height: 0.84;
  }

  .page-header h1 span {
    color: var(--accent-ink);
  }

  .lede {
    max-width: 38rem;
    margin: 0;
    color: var(--ink-2);
    font-size: clamp(1rem, 1.4vw, 1.3rem);
    line-height: 1.55;
  }

  .demo-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.25fr) minmax(24rem, 0.75fr);
    gap: clamp(1rem, 2vw, 2rem);
    max-width: 90rem;
    margin: 0 auto;
    align-items: start;
  }

  .chat-card,
  .metrics-card {
    border: 1px solid var(--stroke);
    background: var(--card-bg);
  }

  .chat-card {
    display: grid;
    grid-template-rows: auto auto minmax(24rem, 1fr) auto;
    min-height: min(48rem, calc(100vh - 8rem));
  }

  .card-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.25rem 1.4rem;
    border-bottom: 1px solid var(--line);
  }

  .card-heading h2 {
    margin: 0.25rem 0 0;
    font-size: 1.35rem;
    font-weight: 550;
    letter-spacing: -0.025em;
  }

  .heading-controls {
    display: flex;
    align-items: center;
    gap: 0.65rem;
  }

  .thinking-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    min-height: 2rem;
    padding: 0 0.55rem;
    border: 1px solid var(--line);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.67rem;
    white-space: nowrap;
    cursor: pointer;
  }

  .thinking-toggle input {
    width: 0.85rem;
    height: 0.85rem;
    margin: 0;
    accent-color: var(--accent-ink);
  }

  .state {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    padding: 0.4rem 0.55rem;
    border: 1px solid var(--line);
    color: var(--ink-2);
  }

  .state-dot {
    width: 0.5rem;
    height: 0.5rem;
    background: var(--ink-3);
    border-radius: 50%;
  }

  .state[data-state="prefilling"] .state-dot {
    background: var(--tone-disk);
    animation: pulse 1.2s ease-in-out infinite;
  }

  .state[data-state="generating"] .state-dot {
    background: var(--ok);
    animation: pulse 1.2s ease-in-out infinite;
  }

  @keyframes pulse {
    50% {
      opacity: 0.35;
    }
  }

  .turn-strip {
    display: grid;
    grid-template-columns: repeat(6, minmax(0, 1fr));
    border-bottom: 1px solid var(--line);
    background: var(--band);
  }

  .turn-strip > div:nth-child(-n + 2) {
    grid-column: span 3;
  }

  .turn-strip > div:nth-child(n + 3) {
    grid-column: span 2;
    border-top: 1px solid var(--line);
  }

  .turn-strip > div {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 0.7rem;
    padding: 0.7rem 1.4rem;
  }

  .turn-strip > div + div {
    border-left: 1px solid var(--line);
  }

  .turn-strip > div:nth-child(3) {
    border-left: 0;
  }

  .turn-strip span {
    color: var(--ink-3);
    font-size: 0.72rem;
  }

  .turn-strip strong,
  .metric-grid dd,
  .vram-hero strong,
  .hardware-facts dd {
    font-family: var(--font-code);
    font-variant-numeric: tabular-nums;
  }

  .turn-strip strong {
    min-width: 5.5ch;
    text-align: right;
    white-space: nowrap;
    font-size: 0.9rem;
  }

  .messages {
    min-height: 0;
    max-height: 54vh;
    overflow-y: auto;
    padding: 1.4rem;
    scrollbar-color: var(--ink-3) transparent;
  }

  .empty-state {
    display: grid;
    place-content: center;
    min-height: 100%;
    max-width: 34rem;
    margin: auto;
    text-align: center;
  }

  .empty-index {
    margin: 0 0 0.8rem;
    color: var(--accent-ink);
    font-family: var(--font-code);
    font-size: 0.75rem;
    letter-spacing: 0.14em;
  }

  .empty-state h3 {
    margin: 0;
    font-size: clamp(1.5rem, 3vw, 2.5rem);
    font-weight: 500;
    letter-spacing: -0.045em;
  }

  .empty-state p:last-child {
    margin: 0.75rem 0 0;
    color: var(--ink-2);
    line-height: 1.6;
  }

  .message {
    max-width: 86%;
    margin: 0 0 1.35rem auto;
    padding: 0.9rem 1rem;
    border: 1px solid var(--stroke);
    background: var(--band);
  }

  .message.assistant {
    margin-right: auto;
    margin-left: 0;
    background: var(--sheet);
  }

  .message-role {
    margin-bottom: 0.45rem;
  }

  .message pre,
  .waiting {
    margin: 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 0.95rem;
    line-height: 1.58;
  }

  .thinking-region {
    margin: 0 0 0.8rem;
    padding: 0.55rem 0.65rem 0.65rem;
    border: 1px solid var(--line);
    background: var(--band);
    color: var(--ink-3);
  }

  .thinking-region summary {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.67rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
  }

  .thinking-region pre {
    margin-top: 0.5rem;
    color: var(--ink-3);
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .answer-region {
    color: var(--ink);
  }

  .waiting {
    color: var(--ink-2);
  }

  .waiting-mark {
    display: inline-block;
    width: 0.48rem;
    height: 0.48rem;
    margin-right: 0.4rem;
    border-radius: 50%;
    background: var(--tone-disk);
    animation: pulse 1.2s ease-in-out infinite;
  }

  .composer {
    padding: 1rem 1.4rem 1.2rem;
    border-top: 1px solid var(--line);
    background: var(--sheet);
  }

  .composer label {
    display: block;
    margin-bottom: 0.45rem;
  }

  .composer textarea {
    display: block;
    width: 100%;
    resize: vertical;
    padding: 0.8rem;
    border: 1px solid var(--stroke);
    border-radius: 0;
    outline: none;
    background: var(--card-bg);
    color: var(--ink);
    font: inherit;
    line-height: 1.45;
  }

  .composer textarea:focus {
    border-color: var(--accent-ink);
    box-shadow: inset 0 -2px 0 var(--accent-ink);
  }

  .composer textarea:disabled {
    color: var(--ink-3);
  }

  .composer-actions {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-top: 0.7rem;
  }

  .composer-actions p {
    margin: 0;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.65rem;
  }

  .send-button,
  .stop-button {
    min-width: 7rem;
    padding: 0.65rem 0.9rem;
    border: 1px solid var(--ink);
    border-radius: 0;
    background: var(--ink);
    color: var(--sheet);
    font-family: var(--font-code);
    font-size: 0.7rem;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    cursor: pointer;
  }

  .send-button:disabled {
    border-color: var(--line);
    background: var(--band);
    color: var(--ink-3);
    cursor: default;
  }

  .stop-button {
    border-color: var(--tone-disk);
    background: transparent;
    color: var(--tone-disk);
  }

  .metrics-card {
    position: sticky;
    top: 1rem;
  }

  .hardware-header {
    padding: 1.3rem 1.4rem 1.4rem;
    border-bottom: 1px solid var(--stroke);
    background: var(--ink);
    color: var(--sheet);
  }

  .hardware-topline {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .hardware-header .section-label {
    color: var(--ink-3);
  }

  .setup-callout {
    margin: 0.9rem 0 1.1rem;
    padding: 0.85rem 0;
    border-top: 1px solid var(--accent-ink);
    border-bottom: 1px solid var(--accent-ink);
    color: var(--sheet);
    font-size: clamp(1rem, 1.7vw, 1.25rem);
    font-weight: 650;
    line-height: 1.35;
  }

  .live-tag {
    color: var(--ok);
  }

  .live-tag::before {
    content: "";
    display: inline-block;
    width: 0.42rem;
    height: 0.42rem;
    margin-right: 0.4rem;
    border-radius: 50%;
    background: currentColor;
    vertical-align: 0.05rem;
  }

  .gpu-name {
    margin: 0.9rem 0 0;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .vram-hero {
    display: flex;
    align-items: baseline;
    gap: 0.8rem;
    margin-top: 0.2rem;
  }

  .vram-hero strong {
    font-size: clamp(2.8rem, 5vw, 4.5rem);
    font-weight: 500;
    letter-spacing: -0.07em;
    line-height: 1;
    white-space: nowrap;
  }

  .vram-hero span,
  .vram-total {
    color: var(--ink-3);
    font-size: 0.72rem;
  }

  .vram-total {
    margin: 0.25rem 0 1.2rem;
    font-family: var(--font-code);
  }

  .hardware-facts {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0;
    margin: 0;
    border-top: 1px solid var(--ink-2);
    border-left: 1px solid var(--ink-2);
  }

  .hardware-facts > div {
    min-width: 0;
    padding: 0.55rem 0.65rem;
    border-right: 1px solid var(--ink-2);
    border-bottom: 1px solid var(--ink-2);
  }

  .hardware-facts dt,
  .metric-grid dt {
    color: var(--ink-3);
    font-size: 0.62rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .hardware-facts dd {
    overflow: hidden;
    margin: 0.18rem 0 0;
    font-size: 0.72rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .stats-error {
    margin: 0;
    padding: 0.7rem 1.4rem;
    border-bottom: 1px solid var(--line);
    color: var(--tone-disk);
    font-family: var(--font-code);
    font-size: 0.68rem;
  }

  .metric-group {
    border-bottom: 1px solid var(--stroke);
  }

  .metric-group h3 {
    display: flex;
    justify-content: space-between;
    margin: 0;
    padding: 0.65rem 1.4rem;
    border-bottom: 1px solid var(--line);
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.62rem;
    font-weight: 500;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }

  .metric-group h3 span {
    color: var(--ink);
    font-weight: 700;
  }

  .current-group h3 span {
    color: var(--accent-ink);
  }

  .metric-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    margin: 0;
  }

  .metric-grid > div {
    min-width: 0;
    padding: 0.72rem 1.4rem 0.85rem;
    border-bottom: 1px solid var(--line);
  }

  .metric-grid > div:nth-child(odd) {
    border-right: 1px solid var(--line);
  }

  .metric-grid > div:nth-last-child(-n + 2) {
    border-bottom: 0;
  }

  .metric-grid dd {
    margin: 0.1rem 0 0;
    font-size: 1.15rem;
    font-weight: 600;
    white-space: nowrap;
  }

  .metric-grid small {
    color: var(--ink-3);
    font-size: 0.62rem;
    font-weight: 400;
  }

  .panel-note {
    margin: 0;
    padding: 0.85rem 1.4rem;
    color: var(--ink-3);
    font-size: 0.65rem;
    line-height: 1.5;
  }

  button:focus-visible,
  textarea:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }

  @media (max-width: 1000px) {
    .page-header,
    .demo-grid {
      grid-template-columns: 1fr;
    }

    .page-header h1 {
      max-width: none;
    }

    .metrics-card {
      position: static;
    }
  }

  @media (max-width: 600px) {
    .demo-page {
      padding: 1rem;
    }

    .page-header {
      gap: 1rem;
    }

    .chat-card {
      grid-template-rows: auto auto minmax(20rem, 1fr) auto;
    }

    .card-heading,
    .composer,
    .messages {
      padding-right: 1rem;
      padding-left: 1rem;
    }

    .turn-strip {
      grid-template-columns: 1fr;
    }

    .turn-strip > div:nth-child(n) {
      grid-column: span 1;
    }

    .turn-strip > div + div {
      border-top: 1px solid var(--line);
      border-left: 0;
    }

    .heading-controls {
      align-items: flex-end;
      flex-direction: column-reverse;
    }

    .message {
      max-width: 94%;
    }

    .composer-actions {
      align-items: flex-end;
    }

    .composer-actions p {
      max-width: 12rem;
    }

    .vram-hero {
      display: block;
    }

    .vram-hero span {
      display: block;
      margin-top: 0.25rem;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .state-dot,
    .waiting-mark {
      animation: none;
    }
  }
</style>
