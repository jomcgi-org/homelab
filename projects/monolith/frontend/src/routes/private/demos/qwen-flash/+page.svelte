<script>
  import { onMount } from "svelte";
  // This demo deliberately uses the public blog tokens because it is presented
  // beside the post, not as part of the private dashboard design system.
  import "$lib/public/styles/technical-drawing.css";
  import {
    calculateTierSummary,
    calculateTurnMetrics,
    classifyLayerTier,
    decayActivity,
    deriveModelState,
    diffExpertHits,
    formatBytes,
    formatRate,
  } from "./metrics.js";

  const CHAT_URL = "/private/demos/qwen-flash/chat";
  const STATS_URL = "/private/demos/qwen-flash/stats";
  const PROFILE_URL = "/private/demos/qwen-flash/profile";
  const CACHE_STATUS_URL = "/private/demos/qwen-flash/cache-status";
  const DEFAULT_LAYER_COUNT = 48;
  const DEFAULT_EXPERT_COUNT = 512;
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
  let thinkingEnabled = $state(true);
  let stats = $state(null);
  let cacheStatus = $state(null);
  let statsError = $state("");
  let profileError = $state("");
  let cacheError = $state("");
  let turnMetrics = $state({ ...EMPTY_TURN_METRICS });
  let turnStartedAt = 0;
  let turnChunks = [];
  let controller;
  let chatLog;
  let expertCanvas;
  let canvasContext;
  let canvasFrame;
  let canvasResizeObserver;
  let themeObserver;
  let profileSnapshot;
  let activity = new Float32Array(DEFAULT_LAYER_COUNT * DEFAULT_EXPERT_COUNT);
  let gridLayers = $state(DEFAULT_LAYER_COUNT);
  let gridExperts = $state(DEFAULT_EXPERT_COUNT);
  let statsTimer;
  let profileTimer;
  let statsPollGeneration = 0;
  let profilePollGeneration = 0;
  let profileRequestPending = false;
  let cacheRequestPending = false;
  let mounted = false;

  let modelState = $derived(deriveModelState(isInFlight, firstTokenSeen));
  let tierSummary = $derived(calculateTierSummary(cacheStatus?.geometry));
  let telemetryError = $derived(profileError || cacheError || statsError);

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

  function scrollToBottom() {
    requestAnimationFrame(() => {
      if (chatLog) chatLog.scrollTop = chatLog.scrollHeight;
    });
  }

  async function pollStats() {
    try {
      const response = await fetch(STATS_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      stats = await response.json();
      statsError = "";
    } catch {
      statsError = "Stats offline";
    }
  }

  function scheduleStats(delay) {
    if (!mounted) return;
    clearTimeout(statsTimer);
    const generation = ++statsPollGeneration;
    statsTimer = setTimeout(async () => {
      await pollStats();
      if (mounted && generation === statsPollGeneration) {
        scheduleStats(isInFlight ? 1000 : 5000);
      }
    }, delay);
  }

  function profileDimensions(profile) {
    const configuredLayers = cacheStatus?.geometry?.num_moe_layers;
    const configuredExperts = cacheStatus?.geometry?.num_experts;
    const layerKeys = Object.keys(profile?.expert_hits ?? {})
      .map(Number)
      .filter((value) => Number.isInteger(value) && value >= 0);
    const inferredLayers = layerKeys.length ? Math.max(...layerKeys) + 1 : 0;
    const inferredExperts = Object.values(profile?.expert_hits ?? {}).reduce(
      (largest, hits) =>
        Array.isArray(hits) ? Math.max(largest, hits.length) : largest,
      0,
    );
    return {
      layers:
        Number.isInteger(configuredLayers) && configuredLayers > 0
          ? configuredLayers
          : inferredLayers || DEFAULT_LAYER_COUNT,
      experts:
        Number.isInteger(configuredExperts) && configuredExperts > 0
          ? configuredExperts
          : inferredExperts || DEFAULT_EXPERT_COUNT,
    };
  }

  function updateGridDimensions(profile) {
    const next = profileDimensions(profile);
    if (next.layers === gridLayers && next.experts === gridExperts) return;
    gridLayers = next.layers;
    gridExperts = next.experts;
    activity = new Float32Array(gridLayers * gridExperts);
  }

  function canvasColor(property) {
    return getComputedStyle(expertCanvas).getPropertyValue(property).trim();
  }

  function drawExpertGrid() {
    if (!canvasContext || !expertCanvas) return;
    const width = expertCanvas.clientWidth;
    const height = expertCanvas.clientHeight;
    canvasContext.clearRect(0, 0, width, height);
    canvasContext.fillStyle = canvasColor("--card-bg") || "transparent";
    canvasContext.fillRect(0, 0, width, height);
    if (!profileSnapshot) return;

    const cellWidth = width / gridExperts;
    const cellHeight = height / gridLayers;
    const gapX = Math.min(0.55, cellWidth * 0.2);
    const gapY = Math.min(0.7, cellHeight * 0.18);
    const residentColor = canvasColor("--accent");
    const diskColor = canvasColor("--ink-3");
    const activeColor = canvasColor("--tone-gpu");

    for (let layer = 0; layer < gridLayers; layer += 1) {
      const baselineColor =
        classifyLayerTier(profileSnapshot.layers, layer) === "disk"
          ? diskColor
          : residentColor;
      for (let expert = 0; expert < gridExperts; expert += 1) {
        const x = expert * cellWidth + gapX / 2;
        const y = layer * cellHeight + gapY / 2;
        const width = Math.max(0.35, cellWidth - gapX);
        const height = Math.max(0.35, cellHeight - gapY);
        canvasContext.globalAlpha = 0.72;
        canvasContext.fillStyle = baselineColor;
        canvasContext.fillRect(x, y, width, height);
        const intensity = activity[layer * gridExperts + expert];
        if (intensity > 0) {
          canvasContext.globalAlpha = 0.35 + intensity * 0.65;
          canvasContext.fillStyle = activeColor;
          canvasContext.fillRect(x, y, width, height);
        }
      }
    }
    canvasContext.globalAlpha = 1;
  }

  function animateActivity() {
    let hasActivity = false;
    for (let index = 0; index < activity.length; index += 1) {
      activity[index] = decayActivity(activity[index]);
      if (activity[index] > 0) hasActivity = true;
    }
    drawExpertGrid();
    if (hasActivity) {
      canvasFrame = requestAnimationFrame(animateActivity);
    } else {
      canvasFrame = undefined;
    }
  }

  function pulseExperts(fired) {
    for (const { layer, expert, delta } of fired) {
      if (layer >= gridLayers || expert >= gridExperts) continue;
      const index = layer * gridExperts + expert;
      const strength = Math.min(1, 0.55 + Math.log2(delta + 1) * 0.12);
      activity[index] = Math.max(activity[index], strength);
    }
    if (fired.length && !canvasFrame) {
      canvasFrame = requestAnimationFrame(animateActivity);
    } else {
      drawExpertGrid();
    }
  }

  function resizeCanvas() {
    if (!expertCanvas || !canvasContext) return;
    const ratio = window.devicePixelRatio || 1;
    const width = expertCanvas.clientWidth;
    const height = expertCanvas.clientHeight;
    expertCanvas.width = Math.max(1, Math.round(width * ratio));
    expertCanvas.height = Math.max(1, Math.round(height * ratio));
    canvasContext.setTransform(ratio, 0, 0, ratio, 0, 0);
    drawExpertGrid();
  }

  async function pollProfile() {
    if (profileRequestPending) return;
    profileRequestPending = true;
    try {
      const response = await fetch(PROFILE_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const next = await response.json();
      updateGridDimensions(next);
      const fired = diffExpertHits(profileSnapshot, next);
      profileSnapshot = next;
      pulseExperts(fired);
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
      if (!cacheStatus) void pollCacheStatus();
      if (mounted && generation === profilePollGeneration) {
        scheduleProfile(isInFlight ? 1000 : 3000);
      }
    }, delay);
  }

  async function pollCacheStatus() {
    if (cacheRequestPending) return;
    cacheRequestPending = true;
    try {
      const response = await fetch(CACHE_STATUS_URL, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      cacheStatus = await response.json();
      updateGridDimensions(profileSnapshot);
      drawExpertGrid();
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
        if (!assistant.content) assistant.content = "Generation stopped.";
      } else {
        assistant.content += `${assistant.content ? "\n\n" : ""}Connection error: ${error.message}`;
      }
    } finally {
      isInFlight = false;
      controller = undefined;
      scheduleStats(0);
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
    canvasContext = expertCanvas.getContext("2d");
    canvasResizeObserver = new ResizeObserver(resizeCanvas);
    canvasResizeObserver.observe(expertCanvas);
    themeObserver = new MutationObserver(drawExpertGrid);
    themeObserver.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    resizeCanvas();
    scheduleStats(0);
    scheduleProfile(0);
    void pollCacheStatus();

    return () => {
      mounted = false;
      clearTimeout(statsTimer);
      clearTimeout(profileTimer);
      if (canvasFrame) cancelAnimationFrame(canvasFrame);
      canvasResizeObserver.disconnect();
      themeObserver.disconnect();
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
    <h1>125B parameters. <span>One RTX 4090.</span></h1>
    <div class="state" data-state={modelState} aria-live="polite">
      <span class="state-dot"></span>
      {modelState}
    </div>
  </header>

  <div class="demo-stack">
    <section class="expert-card" aria-labelledby="expert-heading">
      <header class="expert-heading">
        <div class="expert-title">
          <p class="section-label">01 / Expert residency</p>
          <h2 id="expert-heading">
            {formatCount(tierSummary.totalExperts)} experts
          </h2>
          <div class="legend" aria-label="Expert grid legend">
            <span><i class="resident"></i>Resident layer</span>
            <span><i class="disk"></i>NVMe layer</span>
            <span><i class="active"></i>Active</span>
          </div>
        </div>

        <dl class="tier-summary">
          <div>
            <dt>GPU cache</dt>
            <dd>{formatCount(tierSummary.gpuExperts)}</dd>
          </div>
          <div>
            <dt>NVMe</dt>
            <dd>{formatCount(tierSummary.nvmeExperts)}</dd>
          </div>
          <div>
            <dt>GPU weights</dt>
            <dd>{formatOptionalBytes(tierSummary.gpuBytes)}</dd>
          </div>
          <div>
            <dt>NVMe weights</dt>
            <dd>{formatOptionalBytes(tierSummary.nvmeBytes)}</dd>
          </div>
        </dl>
      </header>

      <div class="canvas-wrap">
        <canvas
          bind:this={expertCanvas}
          aria-label={`${gridLayers} mixture-of-experts layers with ${gridExperts} experts in each row`}
        >
          {gridLayers} layers with {gridExperts} experts in each layer
        </canvas>
        {#if telemetryError}
          <span class="telemetry-error" role="status">{telemetryError}</span>
        {/if}
      </div>
    </section>

    <section class="chat-card" aria-label="Chat with Qwen3.8-Flash-Next">
      <header class="chat-heading">
        <div>
          <p class="section-label">02 / Chat</p>
          <h2>Ask Qwen</h2>
        </div>
        <dl class="turn-metrics">
          <div>
            <dt>Decode</dt>
            <dd>{formatRate(stats?.throughput?.decode_tps)} tok/s</dd>
          </div>
          <div>
            <dt>First token</dt>
            <dd>{formatDuration(turnMetrics.ttftMs)}</dd>
          </div>
        </dl>
        <label class="thinking-toggle">
          <input type="checkbox" bind:checked={thinkingEnabled} />
          <span>Thinking</span>
        </label>
      </header>

      <div class="messages" bind:this={chatLog} aria-live="polite">
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
        <label class="sr-only" for="qwen-prompt">Message</label>
        <textarea
          id="qwen-prompt"
          bind:value={inputText}
          onkeydown={onInputKeydown}
          placeholder="Ask Qwen..."
          rows="2"
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
  </div>
</main>

<style>
  .demo-page {
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
  .demo-stack {
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
    font-size: clamp(2.2rem, 4vw, 4rem);
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
  .tier-summary dt,
  .turn-metrics dt {
    margin: 0;
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.64rem;
    font-weight: 600;
    letter-spacing: 0.11em;
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
    background: var(--card-bg);
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
    background: var(--tone-gpu);
    animation: pulse 1s ease-in-out infinite;
  }

  @keyframes pulse {
    50% {
      opacity: 0.25;
    }
  }

  .demo-stack {
    display: grid;
    grid-template-rows: minmax(20rem, 1.25fr) minmax(16rem, 0.75fr);
    gap: 0.8rem;
    min-height: 0;
  }

  .expert-card,
  .chat-card {
    min-height: 0;
    border: 1px solid var(--stroke);
    background: var(--card-bg);
  }

  .expert-card {
    display: grid;
    grid-template-rows: auto minmax(0, 1fr);
  }

  .expert-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 2rem;
    padding: 0.8rem 1rem;
    border-bottom: 1px solid var(--line);
  }

  .expert-title h2,
  .chat-heading h2 {
    margin: 0.18rem 0 0;
    font-size: 1.25rem;
    font-weight: 550;
    letter-spacing: -0.03em;
  }

  .legend {
    display: flex;
    gap: 1rem;
    margin-top: 0.45rem;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.62rem;
  }

  .legend span {
    display: inline-flex;
    align-items: center;
    gap: 0.32rem;
  }

  .legend i {
    display: block;
    width: 0.55rem;
    height: 0.55rem;
    background: var(--accent);
  }

  .legend .disk {
    background: var(--ink-3);
  }

  .legend .active {
    background: var(--tone-gpu);
  }

  .tier-summary,
  .turn-metrics {
    display: grid;
    margin: 0;
  }

  .tier-summary {
    grid-template-columns: repeat(4, minmax(7rem, 1fr));
    border-left: 1px solid var(--line);
  }

  .tier-summary > div {
    padding: 0.25rem 0.8rem;
    border-right: 1px solid var(--line);
  }

  .tier-summary dd,
  .turn-metrics dd {
    margin: 0.18rem 0 0;
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 1rem;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .canvas-wrap {
    position: relative;
    min-height: 0;
    padding: 0.75rem 1rem 0.9rem;
  }

  canvas {
    display: block;
    width: 100%;
    height: 100%;
  }

  .telemetry-error {
    position: absolute;
    right: 1rem;
    bottom: 0.9rem;
    padding: 0.3rem 0.45rem;
    background: var(--card-bg);
    color: var(--tone-disk);
    font-family: var(--font-code);
    font-size: 0.62rem;
  }

  .chat-card {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(18rem, 0.38fr);
    grid-template-rows: auto minmax(0, 1fr);
  }

  .chat-heading {
    display: grid;
    grid-column: 1 / -1;
    grid-template-columns: minmax(10rem, 1fr) auto auto;
    align-items: center;
    gap: 1rem;
    padding: 0.65rem 1rem;
    border-bottom: 1px solid var(--line);
  }

  .turn-metrics {
    grid-template-columns: repeat(2, minmax(8rem, auto));
    border-left: 1px solid var(--line);
  }

  .turn-metrics > div {
    padding: 0.1rem 0.9rem;
    border-right: 1px solid var(--line);
  }

  .thinking-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.42rem;
    min-height: 2rem;
    padding: 0 0.6rem;
    border: 1px solid var(--line);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.67rem;
    cursor: pointer;
  }

  .thinking-toggle input {
    width: 0.85rem;
    height: 0.85rem;
    margin: 0;
    accent-color: var(--accent-ink);
  }

  .messages {
    min-height: 0;
    overflow-y: auto;
    padding: 0.8rem 1rem;
    border-right: 1px solid var(--line);
    scrollbar-color: var(--ink-3) transparent;
  }

  .message {
    max-width: 88%;
    margin: 0 0 0.65rem auto;
    padding: 0.55rem 0.7rem;
    border: 1px solid var(--line);
    background: var(--band);
  }

  .message.assistant {
    margin-right: auto;
    margin-left: 0;
    background: var(--sheet);
  }

  .message-role {
    margin-bottom: 0.25rem;
  }

  .message pre,
  .waiting {
    margin: 0;
    overflow-wrap: anywhere;
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 0.86rem;
    line-height: 1.42;
    white-space: pre-wrap;
  }

  .thinking-region {
    margin-bottom: 0.45rem;
    padding: 0.4rem 0.5rem;
    border-left: 2px solid var(--line);
    color: var(--ink-3);
  }

  .thinking-region summary {
    color: var(--ink-3);
    font-family: var(--font-code);
    font-size: 0.62rem;
    text-transform: uppercase;
    cursor: pointer;
  }

  .thinking-region pre {
    margin-top: 0.35rem;
    color: var(--ink-3);
    font-size: 0.76rem;
  }

  .waiting {
    color: var(--ink-2);
  }

  .waiting span {
    display: inline-block;
    width: 0.42rem;
    height: 0.42rem;
    margin-right: 0.4rem;
    border-radius: 50%;
    background: var(--tone-gpu);
    animation: pulse 1s ease-in-out infinite;
  }

  .composer {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: stretch;
    gap: 0.6rem;
    min-height: 0;
    padding: 0.8rem;
    background: var(--sheet);
  }

  .composer textarea {
    display: block;
    width: 100%;
    min-height: 0;
    resize: none;
    padding: 0.65rem;
    border: 1px solid var(--stroke);
    border-radius: 0;
    outline: none;
    background: var(--card-bg);
    color: var(--ink);
    font: inherit;
    font-size: 0.88rem;
    line-height: 1.4;
  }

  .composer textarea:focus {
    border-color: var(--accent-ink);
    box-shadow: inset 0 -2px 0 var(--accent-ink);
  }

  .send-button,
  .stop-button {
    min-width: 4.8rem;
    padding: 0.55rem 0.7rem;
    border: 1px solid var(--accent-ink);
    border-radius: 0;
    background: var(--accent-ink);
    color: var(--sheet);
    font-family: var(--font-code);
    font-size: 0.68rem;
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

    .demo-stack {
      grid-template-rows: 26rem minmax(28rem, auto);
    }

    .expert-heading {
      align-items: flex-start;
    }

    .tier-summary {
      grid-template-columns: repeat(2, minmax(7rem, 1fr));
    }

    .chat-card {
      grid-template-columns: 1fr;
      grid-template-rows: auto minmax(16rem, 1fr) auto;
    }

    .messages {
      border-right: 0;
      border-bottom: 1px solid var(--line);
    }
  }

  @media (max-width: 620px) {
    .page-header {
      align-items: flex-end;
    }

    .page-header h1 {
      font-size: 2rem;
    }

    .expert-heading,
    .chat-heading {
      display: flex;
      flex-wrap: wrap;
    }

    .tier-summary {
      width: 100%;
      border-top: 1px solid var(--line);
    }

    .turn-metrics {
      margin-left: auto;
    }
  }
</style>
