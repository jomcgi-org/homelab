<script>
  import { onMount } from "svelte";
  import { marked } from "marked";
  // This demo deliberately uses the public blog tokens because it is presented
  // beside the post, not as part of the private dashboard design system.
  import "$lib/public/styles/technical-drawing.css";
  import {
    addSessionTurn,
    attributeExpertActivity,
    calculateTierSummary,
    calculateTurnMetrics,
    deriveModelState,
    formatBytes,
    formatRate,
    trackSessionPeaks,
  } from "./metrics.js";

  const CHAT_URL = "/private/demos/qwen-flash/chat";
  const PROFILE_URL = "/private/demos/qwen-flash/profile";
  const CACHE_STATUS_URL = "/private/demos/qwen-flash/cache-status";
  const STATS_URL = "/private/demos/qwen-flash/stats";
  const EMPTY_TURN_METRICS = {
    ttftMs: null,
    tokensPerSecond: 0,
    timeToFirstReasoningMs: null,
    timeToFirstAnswerMs: null,
    reasoningTokens: 0,
    answerTokens: 0,
  };
  const EMPTY_ACTIVITY = {
    hotHits: 0,
    warmHits: 0,
    coldHits: 0,
    unknownHits: 0,
    totalHits: 0,
  };

  let messages = $state([]);
  let inputText = $state("");
  let isInFlight = $state(false);
  let firstTokenSeen = $state(false);
  // This drives generation. It is not only a display preference.
  let thinkingEnabled = $state(true);
  let displayProfile = $state(null);
  let cacheStatus = $state(null);
  let activity = $state({ ...EMPTY_ACTIVITY });
  let sessionPeaks = $state({ decodeTps: 0, ttftMs: null });
  let sessionTotals = $state({ turns: 0, tokens: 0, generationMs: 0 });
  let profileError = $state("");
  let cacheError = $state("");
  let turnMetrics = $state({ ...EMPTY_TURN_METRICS });
  let turnStartedAt = 0;
  let turnChunks = [];
  let controller;
  let chatLog;
  let profileSnapshot;
  let cacheSnapshot;
  let profileLayerSignature = "";
  let cacheGeometrySignature = "";
  let pendingVisual = {};
  let visualFrame;
  let profileTimer;
  let profilePollGeneration = 0;
  let profileRequestPending = false;
  let cacheRequestPending = false;
  let mounted = false;

  let modelState = $derived(deriveModelState(isInFlight, firstTokenSeen));
  let tierSummary = $derived(
    calculateTierSummary(displayProfile, cacheStatus?.geometry),
  );
  let telemetryError = $derived(profileError || cacheError);

  function formatCount(value) {
    return value === null || value === undefined
      ? "--"
      : Math.max(0, Number(value) || 0).toLocaleString("en-US");
  }

  function formatOptionalBytes(value) {
    return value === null || value === undefined ? "--" : formatBytes(value);
  }

  function formatDuration(milliseconds) {
    return milliseconds === null
      ? "--.- s"
      : `${(milliseconds / 1000).toFixed(1)} s`;
  }

  function formatTotalDuration(milliseconds) {
    const seconds = Math.max(0, Number(milliseconds) || 0) / 1000;
    return `${seconds.toFixed(1)} s`;
  }

  function percentage(value, total) {
    if (
      value === null ||
      total === null ||
      !Number.isFinite(value) ||
      !Number.isFinite(total) ||
      total <= 0
    ) {
      return 0;
    }
    return Math.max(0, Math.min(100, (value / total) * 100));
  }

  function formatPercentage(value, total) {
    return total > 0 ? `${percentage(value, total).toFixed(0)}%` : "--";
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(
      /[&<>]/g,
      (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[character],
    );
  }

  function renderMarkdown(value) {
    return marked.parse(escapeHtml(value));
  }

  function scrollToBottom() {
    requestAnimationFrame(() => {
      if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
    });
  }

  function scheduleVisual(update) {
    pendingVisual = { ...pendingVisual, ...update };
    if (visualFrame) return;
    visualFrame = requestAnimationFrame(() => {
      const next = pendingVisual;
      pendingVisual = {};
      visualFrame = undefined;
      if ("profile" in next) displayProfile = next.profile;
      if ("cache" in next) cacheStatus = next.cache;
      if ("activity" in next) activity = next.activity;
    });
  }

  async function pollProfile() {
    if (profileRequestPending) return;
    profileRequestPending = true;
    try {
      const response = await fetch(PROFILE_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = await response.json();
      const nextActivity = attributeExpertActivity(
        profileSnapshot,
        next,
        cacheSnapshot?.geometry,
      );
      profileSnapshot = next;

      const nextLayerSignature = JSON.stringify(next?.layers ?? null);
      const visualUpdate = {};
      if (nextLayerSignature !== profileLayerSignature) {
        profileLayerSignature = nextLayerSignature;
        visualUpdate.profile = next;
      }
      if (nextActivity.totalHits > 0) visualUpdate.activity = nextActivity;
      if (Object.keys(visualUpdate).length) scheduleVisual(visualUpdate);
      profileError = "";
    } catch {
      profileError = "Expert profile offline";
    } finally {
      profileRequestPending = false;
    }
  }

  function scheduleProfile(delay) {
    if (!mounted) return;
    clearTimeout(profileTimer);
    const generation = ++profilePollGeneration;
    profileTimer = setTimeout(async () => {
      await pollProfile();
      void pollStats();
      if (!cacheSnapshot) void pollCacheStatus();
      if (mounted && generation === profilePollGeneration) {
        scheduleProfile(isInFlight ? 1000 : 3000);
      }
    }, delay);
  }

  let serverStats = $state(null);
  let statsRequestPending = false;

  async function pollStats() {
    if (statsRequestPending) return;
    statsRequestPending = true;
    try {
      const response = await fetch(STATS_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      serverStats = await response.json();
    } catch {
      // Leave the last good sample in place: a dropped poll should not blank
      // the panel mid-demo.
    } finally {
      statsRequestPending = false;
    }
  }

  async function pollCacheStatus() {
    if (cacheRequestPending) return;
    cacheRequestPending = true;
    try {
      const response = await fetch(CACHE_STATUS_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = await response.json();
      cacheSnapshot = next;
      const nextSignature = JSON.stringify(next?.geometry ?? null);
      if (nextSignature !== cacheGeometrySignature) {
        cacheGeometrySignature = nextSignature;
        scheduleVisual({ cache: next });
      }
      cacheError = "";
    } catch {
      cacheError = "Cache status offline";
    } finally {
      cacheRequestPending = false;
    }
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
    if (!reasoningContent && !content) return;

    turnChunks = [
      ...turnChunks,
      {
        at: now,
        reasoning_content: reasoningContent,
        content,
      },
    ];
    turnMetrics = calculateTurnMetrics(turnStartedAt, turnChunks);
    sessionPeaks = trackSessionPeaks(sessionPeaks, turnMetrics);
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
      const choice = event.choices?.[0];
      const assistant = messages[messages.length - 1];
      if (typeof choice?.finish_reason === "string") {
        assistant.finishReason = choice.finish_reason;
      }
      if (choice?.delta && typeof choice.delta === "object") {
        appendDelta(choice.delta);
      }
    } catch {
      // A malformed upstream event is ignored without interrupting the stream.
    }
  }

  async function sendMessage() {
    const text = inputText.trim();
    if (!text || isInFlight) return;

    inputText = "";
    messages.push({ role: "user", content: text, reasoningContent: "" });
    messages.push({
      role: "assistant",
      content: "",
      reasoningContent: "",
      finishReason: "",
    });
    isInFlight = true;
    firstTokenSeen = false;
    turnStartedAt = performance.now();
    turnChunks = [];
    turnMetrics = { ...EMPTY_TURN_METRICS };
    controller = new AbortController();
    scheduleProfile(0);
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
        assistant.finishReason = "stopped";
        if (!assistant.content) assistant.content = "Generation stopped.";
      } else {
        assistant.finishReason = "error";
        assistant.content += `${assistant.content ? "\n\n" : ""}Connection error: ${error.message}`;
      }
    } finally {
      sessionTotals = addSessionTurn(
        sessionTotals,
        turnMetrics,
        performance.now() - turnStartedAt,
      );
      isInFlight = false;
      controller = undefined;
      scheduleProfile(0);
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
    scheduleProfile(0);
    void pollCacheStatus();
    void pollStats();

    return () => {
      mounted = false;
      clearTimeout(profileTimer);
      if (visualFrame) cancelAnimationFrame(visualFrame);
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
    <h1>Qwen 3.8 Flash</h1>
    <div class="state" data-state={modelState} aria-live="polite">
      <span class="state-dot"></span>
      {modelState}
    </div>
  </header>

  <div class="demo-grid">
    <section class="chat-card" aria-label="Chat with Qwen3.8-Flash-Next">
      <div class="messages" bind:this={chatLog} aria-live="polite">
        {#if messages.length === 0}
          <div class="empty-state">
            <p>Ready for the first question.</p>
            <span>Thinking uses the model's own generation profile.</span>
          </div>
        {/if}
        {#each messages as message, index}
          <article
            class:assistant={message.role === "assistant"}
            class="message"
          >
            <p class="message-role">{message.role}</p>
            {#if message.role === "assistant" && !message.content && !message.reasoningContent && isInFlight && index === messages.length - 1}
              <p class="waiting"><span></span>Waiting for first token</p>
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
              <div class="answer-region">
                {@html renderMarkdown(message.content)}
              </div>
              {#if false}
                <p class="finish-reason"></p>
              {/if}
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
        <label class="sr-only" for="qwen-prompt">Message</label>
        <label class="thinking-toggle">
          <input type="checkbox" bind:checked={thinkingEnabled} />
          <span>Thinking</span>
        </label>
        <textarea
          id="qwen-prompt"
          bind:value={inputText}
          onkeydown={onInputKeydown}
          placeholder="Ask Qwen..."
          rows="3"
          disabled={isInFlight}></textarea>
        {#if isInFlight}
          <button class="stop-button" type="button" onclick={stopGeneration}>
            Stop
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
      </form>
    </section>

    <aside class="side-column" aria-label="Model telemetry">
      <section class="activity-card" aria-labelledby="activity-heading">
        <header class="card-heading">
          <h2 id="activity-heading">Routing</h2>
          {#if telemetryError}
            <span class="telemetry-error" role="status">{telemetryError}</span>
          {/if}
        </header>
        <div class="activity-heading">
          <div>
            <p class="section-label">Latest routing sample</p>
            <h3>{formatCount(activity.totalHits)} activations</h3>
          </div>
          {#if activity.unknownHits > 0}
            <span>{formatCount(activity.unknownHits)} unclassified</span>
          {/if}
        </div>
        <div
          class="activity-bar"
          aria-label="Latest expert activations by tier"
        >
          <span
            class="hot"
            style:width={`${percentage(activity.hotHits, activity.totalHits)}%`}
          ></span>
          <span
            class="warm"
            style:width={`${percentage(activity.warmHits, activity.totalHits)}%`}
          ></span>
          <span
            class="cold"
            style:width={`${percentage(activity.coldHits, activity.totalHits)}%`}
          ></span>
        </div>
        <div class="activity-values" aria-hidden="true">
          <span class="hot"
            >Hot {formatPercentage(activity.hotHits, activity.totalHits)}</span
          >
          <span class="warm"
            >Warm {formatPercentage(
              activity.warmHits,
              activity.totalHits,
            )}</span
          >
          <span class="cold"
            >Cold {formatPercentage(
              activity.coldHits,
              activity.totalHits,
            )}</span
          >
        </div>
        <p class="method-note">
          Hot activity is estimated from routing frequency.
        </p>
      </section>

      <section class="stats-card" aria-labelledby="stats-heading">
        <header class="card-heading">
          <h2 id="stats-heading">Perf</h2>
        </header>
        <table class="perf-table">
          <tbody>
            <tr class="group"><th colspan="2" scope="rowgroup">Live</th></tr>
            <tr>
              <th scope="row">Decode</th>
              <td>{formatRate(turnMetrics.tokensPerSecond)} tok/s</td>
            </tr>
            <tr>
              <th scope="row">KV pages</th>
              <td
                >{formatCount(serverStats?.kv?.used_pages ?? 0)} / {formatCount(
                  serverStats?.kv?.total_pages ?? 0,
                )}</td
              >
            </tr>
            <tr>
              <th scope="row">First token</th>
              <td>{formatDuration(turnMetrics.ttftMs)}</td>
            </tr>
            <tr>
              <th scope="row">In flight</th>
              <td>{formatCount(serverStats?.requests?.active ?? 0)}</td>
            </tr>
            <tr class="group"><th colspan="2" scope="rowgroup">Peak</th></tr>
            <tr>
              <th scope="row">Decode</th>
              <td>{formatRate(sessionPeaks.decodeTps)} tok/s</td>
            </tr>
            <tr>
              <th scope="row">First token</th>
              <td>{formatDuration(sessionPeaks.ttftMs)}</td>
            </tr>
            <tr class="group"><th colspan="2" scope="rowgroup">Session</th></tr>
            <tr>
              <th scope="row">Turns</th>
              <td>{formatCount(sessionTotals.turns)}</td>
            </tr>
            <tr>
              <th scope="row">Tokens</th>
              <td>{formatCount(sessionTotals.tokens)}</td>
            </tr>
            <tr>
              <th scope="row">Generating</th>
              <td>{formatTotalDuration(sessionTotals.generationMs)}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <section class="storage-card" aria-labelledby="storage-heading">
        <header class="card-heading">
          <h2 id="storage-heading">Storage</h2>
        </header>
        <div
          class="capacity-bar"
          aria-label="Expert capacity split between hot, warm, and cold tiers"
        >
          <span
            class="hot"
            style:width={`${percentage(tierSummary.hotExperts, tierSummary.totalExperts)}%`}
          ></span>
          <span
            class="warm"
            style:width={`${percentage(tierSummary.warmExperts, tierSummary.totalExperts)}%`}
          ></span>
          <span
            class="cold"
            style:width={`${percentage(tierSummary.coldExperts, tierSummary.totalExperts)}%`}
          ></span>
        </div>

        <dl class="tier-list">
          <div class="hot">
            <dt><i></i><strong>Hot</strong><span>VRAM</span></dt>
            <dd>
              <strong>{formatCount(tierSummary.hotExperts)}</strong>
              <span>{formatOptionalBytes(tierSummary.hotBytes)}</span>
            </dd>
          </div>
          <div class="warm">
            <dt><i></i><strong>Warm</strong><span>RAM</span></dt>
            <dd>
              <strong>{formatCount(tierSummary.warmExperts)}</strong>
              <span>{formatOptionalBytes(tierSummary.warmBytes)}</span>
            </dd>
          </div>
          <div class="cold">
            <dt><i></i><strong>Cold</strong><span>NVMe</span></dt>
            <dd>
              <strong>{formatCount(tierSummary.coldExperts)}</strong>
              <span>{formatOptionalBytes(tierSummary.coldBytes)}</span>
            </dd>
          </div>
        </dl>
      </section>
    </aside>
  </div>
</main>

<style>
  .demo-page {
    /* Haynes-manual primaries: saturated and unambiguous at projector distance,
       rather than the muted UI tones the rest of the private tier uses. */
    --tier-hot: #e10600;
    --tier-warm: #ffc400;
    --tier-cold: #0057d9;
    /* Cards sit on white. The sheet tone stays as the page ground, so panels
       read as drawn-on-paper rather than washing into it. */
    --panel: #ffffff;
    box-sizing: border-box;
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
    gap: 0.8rem;
    width: 100%;
    height: 100dvh;
    overflow: hidden;
    padding: clamp(0.8rem, 1.8vw, 1.5rem);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
  }

  .page-header,
  .demo-grid {
    width: 100%;
    max-width: 90rem;
    margin: 0 auto;
  }

  .page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding-bottom: 0.65rem;
    border-bottom: 1px solid var(--stroke);
  }

  .page-header h1 {
    margin: 0;
    font-size: clamp(2rem, 3.7vw, 3.7rem);
    font-weight: 500;
    letter-spacing: -0.06em;
    line-height: 0.95;
    white-space: nowrap;
  }

  .page-header h1 span {
    color: var(--accent-ink);
  }

  .section-label,
  .state,
  .message-role,
  .tier-list dt span,
  .stats-groups h3,
  .stats-groups dt,
  .finish-reason {
    margin: 0;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }

  .state {
    display: inline-flex;
    flex: 0 0 auto;
    align-items: center;
    gap: 0.45rem;
    min-height: 2rem;
    padding: 0 0.65rem;
    border: 1px solid var(--line);
    background: var(--panel);
    color: var(--ink-2);
  }

  .state-dot {
    width: 0.48rem;
    height: 0.48rem;
    border-radius: 50%;
    background: var(--ink-3);
  }

  .state[data-state="prefilling"] .state-dot,
  .state[data-state="generating"] .state-dot {
    background: var(--tier-hot);
    animation: pulse 1s ease-in-out infinite;
  }

  @keyframes pulse {
    50% {
      opacity: 0.25;
    }
  }

  .demo-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.9fr) minmax(22rem, 0.82fr);
    gap: 0.8rem;
    min-height: 0;
  }

  .chat-card,
  .activity-card,
  .storage-card,
  .stats-card {
    min-height: 0;
    border: 1px solid var(--stroke);
    background: var(--panel);
  }

  .chat-card {
    display: grid;
    grid-template-rows: minmax(0, 1fr) auto;
  }

  .side-column {
    display: grid;
    grid-template-rows: auto auto auto;
    align-content: start;
    gap: 0.8rem;
    min-height: 0;
  }

  .card-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    padding: 0.4rem 0.9rem;
    border-bottom: 1px solid var(--stroke);
  }

  .side-column .card-heading h2 {
    font-size: 0.95rem;
    margin: 0;
  }

  .card-heading h2 {
    margin: 0.18rem 0 0;
    font-size: 1.28rem;
    font-weight: 550;
    letter-spacing: -0.03em;
  }

  .thinking-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.45rem;
    min-height: 2.2rem;
    padding: 0 0.7rem;
    border: 1px solid var(--line);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    cursor: pointer;
  }

  .thinking-toggle input {
    width: 0.9rem;
    height: 0.9rem;
    margin: 0;
    accent-color: var(--accent-ink);
  }

  .messages {
    min-height: 0;
    overflow-y: auto;
    padding: 1rem;
    scrollbar-color: var(--ink-3) transparent;
  }

  .empty-state {
    display: grid;
    place-content: center;
    height: 100%;
    color: var(--ink-2);
    text-align: center;
  }

  .empty-state p {
    margin: 0 0 0.25rem;
    font-size: 1.15rem;
  }

  .empty-state span {
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .message {
    max-width: 88%;
    margin: 0 0 0.85rem auto;
    padding: 0.72rem 0.82rem;
    border: 1px solid var(--line);
    background: var(--band);
  }

  .message.assistant {
    margin-right: auto;
    margin-left: 0;
    background: var(--sheet);
  }

  .message-role {
    margin-bottom: 0.35rem;
  }

  .message pre,
  .waiting,
  .answer-region {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 1rem;
    line-height: 1.52;
    white-space: pre-wrap;
  }

  .answer-region :global(p),
  .answer-region :global(ul),
  .answer-region :global(ol),
  .answer-region :global(pre),
  .answer-region :global(blockquote) {
    margin: 0 0 0.65em;
  }

  .answer-region :global(:last-child) {
    margin-bottom: 0;
  }

  .answer-region :global(ul),
  .answer-region :global(ol) {
    padding-left: 1.25rem;
  }

  .answer-region :global(code) {
    font-family: var(--font-code);
    font-size: 0.9em;
  }

  .thinking-region {
    margin-bottom: 0.6rem;
    padding: 0.45rem 0.55rem;
    border-left: 2px solid var(--line);
    color: var(--ink-3);
  }

  .thinking-region summary {
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.68rem;
    text-transform: uppercase;
    cursor: pointer;
  }

  .thinking-region pre {
    margin-top: 0.4rem;
    color: var(--ink-3);
    font-size: 0.86rem;
  }

  .finish-reason {
    margin-top: 0.55rem;
  }

  .waiting {
    color: var(--ink-2);
  }

  .waiting span {
    display: inline-block;
    width: 0.45rem;
    height: 0.45rem;
    margin-right: 0.45rem;
    border-radius: 50%;
    background: var(--tier-hot);
    animation: pulse 1s ease-in-out infinite;
  }

  .composer .thinking-toggle {
    align-self: center;
    margin-right: 0.6rem;
    white-space: nowrap;
  }

  .composer {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr) auto;
    align-items: stretch;
    gap: 0.65rem;
    padding: 0.85rem;
    border-top: 1px solid var(--line);
    background: var(--sheet);
  }

  .composer textarea {
    display: block;
    width: 100%;
    min-height: 3.2rem;
    resize: none;
    padding: 0.7rem;
    border: 1px solid var(--stroke);
    border-radius: 0;
    outline: none;
    background: var(--panel);
    color: var(--ink);
    font: inherit;
    font-size: 0.98rem;
    line-height: 1.45;
  }

  .composer textarea:focus {
    border-color: var(--accent-ink);
    box-shadow: inset 0 -1px 0 var(--accent-ink);
  }

  .send-button,
  .stop-button {
    min-width: 5.2rem;
    padding: 0.6rem 0.8rem;
    border: 1px solid var(--accent-ink);
    border-radius: 0;
    background: var(--accent-ink);
    color: var(--sheet);
    font-family: var(--font-code);
    font-size: 0.72rem;
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
    border-color: var(--tier-hot);
    background: transparent;
    color: var(--tier-hot);
  }

  .telemetry-error {
    color: var(--tier-hot);
    font-family: var(--font-code);
    font-size: 0.64rem;
  }

  .capacity-bar,
  .activity-bar {
    display: flex;
    width: calc(100% - 1.8rem);
    height: 1.1rem;
    margin: 1rem 0.9rem 0;
    overflow: hidden;
    background: var(--band);
  }

  .capacity-bar span,
  .activity-bar span {
    display: block;
    height: 100%;
  }

  .hot {
    --tier-color: var(--tier-hot);
  }
  .warm {
    --tier-color: var(--tier-warm);
  }
  .cold {
    --tier-color: var(--tier-cold);
  }
  .capacity-bar .hot,
  .capacity-bar .warm,
  .capacity-bar .cold,
  .activity-bar .hot,
  .activity-bar .warm,
  .activity-bar .cold {
    background: var(--tier-color);
  }

  .tier-list {
    display: grid;
    gap: 0;
    margin: 0.65rem 0.9rem 0;
  }

  .tier-list > div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.7rem;
    padding: 0.5rem 0;
    border-bottom: 1px solid var(--line);
  }

  .tier-list dt,
  .tier-list dd {
    display: flex;
    align-items: baseline;
    gap: 0.45rem;
    margin: 0;
  }

  .tier-list dt i {
    align-self: center;
    width: 0.65rem;
    height: 0.65rem;
    background: var(--tier-color);
  }

  .tier-list dt strong {
    font-size: 0.92rem;
    text-transform: uppercase;
  }

  .tier-list dd strong,
  .tier-list dd span {
    font-family: var(--font-code);
    font-variant-numeric: tabular-nums;
  }

  .tier-list dd strong {
    font-size: 0.92rem;
  }
  .tier-list dd span {
    color: var(--ink-3);
    font-size: 0.7rem;
  }

  .activity-heading {
    display: flex;
    align-items: end;
    justify-content: space-between;
    gap: 0.6rem;
    margin: 0.9rem 0.9rem 0;
  }

  .activity-heading h3 {
    margin: 0.12rem 0 0;
    font-size: 1rem;
  }

  .activity-heading > span {
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.65rem;
  }

  .activity-bar {
    height: 0.78rem;
    margin-top: 0.5rem;
  }

  .activity-values {
    display: flex;
    justify-content: space-between;
    gap: 0.4rem;
    margin: 0.4rem 0.9rem 0;
    font-family: var(--font-code);
    font-size: 0.66rem;
  }

  .activity-values span::before {
    display: inline-block;
    width: 0.45rem;
    height: 0.45rem;
    margin-right: 0.25rem;
    background: var(--tier-color);
    content: "";
  }

  .method-note {
    font-size: 0.66rem;
    line-height: 1.3;
    margin: 0.25rem 0 0;
    margin: 0.75rem 0.9rem 0;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.62rem;
    line-height: 1.4;
  }

  .stats-groups {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    align-content: start;
  }

  .stats-groups section {
    min-width: 0;
    padding: 0.6rem 0.6rem;
    border-right: 1px solid var(--line);
  }

  .stats-groups section:last-child {
    border-right: 0;
  }
  .stats-groups h3 {
    margin-bottom: 0.45rem;
  }
  .stats-groups dl {
    margin: 0;
  }

  .stats-groups dl > div {
    margin-bottom: 0.45rem;
  }

  .stats-groups dd {
    margin: 0.18rem 0 0;
    font-family: var(--font-code);
    font-size: 0.82rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
  }

  @media (max-width: 900px) {
    .demo-page {
      height: auto;
      min-height: 100dvh;
      overflow: visible;
    }

    .page-header h1 {
      white-space: normal;
    }
    .demo-grid {
      grid-template-columns: 1fr;
    }
    .chat-card {
      min-height: 36rem;
    }
    .side-column {
      grid-template-rows: auto auto;
    }
    .activity-card,
    .storage-card,
    .stats-card {
      min-height: 20rem;
    }
  }

  @media (max-width: 540px) {
    .page-header {
      align-items: flex-end;
    }
    .page-header h1 {
      font-size: 2rem;
    }
    .stats-groups {
      grid-template-columns: 1fr;
      height: auto;
    }
    .stats-groups section {
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
  }

  .storage-card .capacity-bar {
    height: 0.5rem;
  }
  .storage-card .tier-list dt strong,
  .storage-card .tier-list dd strong {
    font-size: 0.86rem;
  }
  .storage-card .tier-list > div {
    padding: 0.18rem 0;
  }

  .perf-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.82rem;
  }
  .perf-table th,
  .perf-table td {
    padding: 0.16rem 0.9rem;
    border-bottom: 1px solid var(--line);
    text-align: left;
  }
  .perf-table tr.group th {
    font-family: var(--font-code);
    font-size: 0.7rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--muted-ink);
    background: var(--sheet);
    border-bottom: 1px solid var(--stroke);
  }
  .perf-table tr:not(.group) th {
    font-weight: 400;
    color: var(--muted-ink);
  }
  .perf-table td {
    text-align: right;
    font-family: var(--font-code);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }
  .perf-table tr:last-child th,
  .perf-table tr:last-child td {
    border-bottom: 0;
  }

  /* The tier stylesheet resets list markers globally, which strips the bullets
     from model-authored markdown. Restore them inside the rendered answer only,
     and keep long items wrapping rather than running past the card. */
  .answer-region :global(ul),
  .answer-region :global(ol) {
    margin: 0.5rem 0;
    padding-left: 1.35rem;
    list-style-position: outside;
  }
  .answer-region :global(ul) {
    list-style-type: disc;
  }
  .answer-region :global(ol) {
    list-style-type: decimal;
  }
  .answer-region :global(li) {
    margin: 0.18rem 0;
  }
  .answer-region :global(li)::marker {
    color: var(--muted-ink);
  }
  .answer-region :global(p),
  .answer-region :global(li) {
    overflow-wrap: anywhere;
  }
  .answer-region :global(pre) {
    overflow-x: auto;
    max-width: 100%;
  }
  /* A stray heading from the model must not blow up the layout. */
  .answer-region :global(h1),
  .answer-region :global(h2),
  .answer-region :global(h3) {
    font-size: 1rem;
    margin: 0.6rem 0 0.3rem;
  }
</style>
