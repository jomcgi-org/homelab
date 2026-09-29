<script>
  import { Marked } from "marked";
  import {
    applyChunk,
    formatBytes,
    formatCount,
    formatMs,
    formatRate,
    newTurn,
    parseSseChunk,
    sessionStats,
    turnStats,
  } from "./metrics.js";

  const STORAGE_KEY = "qwen-chat-sessions-v1";
  const STATS_INTERVAL_MS = 5000;

  // Model output is rendered as markdown, with raw HTML shown as text.
  const escapeHtml = (text) =>
    text.replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const markdown = new Marked({
    renderer: { html: ({ text }) => escapeHtml(text) },
  });
  const render = (text) => markdown.parse(text ?? "");

  let dark = $state(
    typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
  $effect(() => {
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => (dark = scheme.matches);
    apply();
    scheme.addEventListener("change", apply);
    return () => scheme.removeEventListener("change", apply);
  });

  function loadSessions() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
      if (Array.isArray(saved) && saved.length) return saved;
    } catch {
      // Storage can be unavailable or hold a stale shape; start fresh.
    }
    return [blankSession()];
  }

  function blankSession() {
    return {
      id: crypto.randomUUID(),
      title: "New chat",
      createdAt: Date.now(),
      messages: [],
    };
  }

  let sessions = $state(loadSessions());
  let activeId = $state(sessions[0].id);
  let active = $derived(sessions.find((s) => s.id === activeId) ?? sessions[0]);
  let input = $state("");
  let thinking = $state(true);
  let maxTokens = $state(8192);
  let streaming = $state(false);
  let controller = null;
  let server = $state(null);
  let serverError = $state("");
  let scroller = $state();

  $effect(() => {
    const snapshot = JSON.stringify(sessions);
    try {
      localStorage.setItem(STORAGE_KEY, snapshot);
    } catch {
      // Quota or privacy mode: the session simply is not persisted.
    }
  });

  let turns = $derived(
    active.messages.filter((m) => m.role === "assistant" && m.turn),
  );
  let session = $derived(sessionStats(turns.map((m) => m.turn)));
  let last = $derived(
    turns.length ? turnStats(turns[turns.length - 1].turn) : null,
  );
  let ctxLimit = $derived(server?.stats?.model?.ctx ?? 100352);

  async function pollStats() {
    try {
      const res = await fetch("/private/qwen/stats");
      const body = await res.json();
      if (!res.ok) throw new Error(body.error ?? `HTTP ${res.status}`);
      server = body;
      serverError = "";
    } catch (err) {
      serverError = err.message;
    }
  }
  $effect(() => {
    pollStats();
    const timer = setInterval(pollStats, STATS_INTERVAL_MS);
    return () => clearInterval(timer);
  });

  function scrollDown() {
    queueMicrotask(() => scroller?.scrollTo({ top: scroller.scrollHeight }));
  }

  async function send() {
    const text = input.trim();
    if (!text || streaming) return;
    input = "";
    const chat = active;
    if (!chat.messages.length) chat.title = text.slice(0, 48);
    chat.messages.push({ role: "user", content: text });
    const history = chat.messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));
    chat.messages.push({
      role: "assistant",
      content: "",
      turn: newTurn(performance.now()),
    });
    const reply = chat.messages[chat.messages.length - 1];
    streaming = true;
    controller = new AbortController();
    scrollDown();
    try {
      const res = await fetch("/private/qwen/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: history,
          enableThinking: thinking,
          maxTokens,
        }),
        signal: controller.signal,
      });
      if (!res.ok || !res.body) {
        const detail = await res.text();
        throw new Error(`HTTP ${res.status}: ${detail.slice(0, 300)}`);
      }
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parsed = parseSseChunk(buffer);
        buffer = parsed.rest;
        for (const event of parsed.events) {
          if (event.json) applyChunk(reply.turn, event.json, performance.now());
        }
        reply.content = reply.turn.content;
        scrollDown();
      }
    } catch (err) {
      reply.turn.error =
        err.name === "AbortError" ? "stopped" : String(err.message ?? err);
    } finally {
      reply.turn.endedAt = performance.now();
      reply.content = reply.turn.content;
      streaming = false;
      controller = null;
      pollStats();
    }
  }

  function stop() {
    controller?.abort();
  }

  function newChat() {
    if (streaming) return;
    const fresh = blankSession();
    sessions.unshift(fresh);
    activeId = fresh.id;
  }

  function removeChat(id) {
    if (streaming) return;
    sessions = sessions.filter((s) => s.id !== id);
    if (!sessions.length) sessions = [blankSession()];
    if (!sessions.some((s) => s.id === activeId)) activeId = sessions[0].id;
  }

  function onKey(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      send();
    }
  }

  const pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);

  // Tiny inline chart: one polyline over the session's turns.
  function sparkline(values, width = 220, height = 44) {
    const kept = values.map((v) => (v == null ? null : v));
    const nums = kept.filter((v) => v != null);
    if (nums.length < 2) return "";
    const max = Math.max(...nums) || 1;
    const step = width / (kept.length - 1);
    return kept
      .map((v, i) =>
        v == null
          ? null
          : `${(i * step).toFixed(1)},${(height - (v / max) * (height - 4) - 2).toFixed(1)}`,
      )
      .filter(Boolean)
      .join(" ");
  }
  let turnSeries = $derived(turns.map((m) => turnStats(m.turn)));
</script>

<svelte:head>
  <title>Qwen | jomcgi</title>
</svelte:head>

<main class="qwen-page shell {dark ? 'night' : 'day'}">
  <aside class="sessions" aria-label="Chat sessions">
    <button class="new" onclick={newChat} disabled={streaming}
      >+ New chat</button
    >
    <ul>
      {#each sessions as s (s.id)}
        <li class:current={s.id === activeId}>
          <button
            class="pick"
            onclick={() => (activeId = s.id)}
            disabled={streaming}
          >
            {s.title}
          </button>
          <button
            class="drop"
            aria-label="Delete {s.title}"
            onclick={() => removeChat(s.id)}
            disabled={streaming}>×</button
          >
        </li>
      {/each}
    </ul>
  </aside>

  <section class="chat" aria-label="Conversation">
    <header class="bar">
      <h1>
        <a class="home" href="/" aria-label="Back to dashboard">←</a> Qwen
        <span class="model"
          >{server?.stats?.model?.id ?? "qwen3.6-27b"} · RTX 4090</span
        >
      </h1>
      <div class="controls">
        <label
          ><input
            type="checkbox"
            bind:checked={thinking}
            disabled={streaming}
          /> thinking</label
        >
        <label>
          max tokens
          <select bind:value={maxTokens} disabled={streaming}>
            {#each [1024, 4096, 8192, 16384, 32768] as n}
              <option value={n}>{n.toLocaleString("en-US")}</option>
            {/each}
          </select>
        </label>
      </div>
    </header>

    <div class="log" bind:this={scroller}>
      {#if !active.messages.length}
        <p class="empty">
          Ask something. Prompts run on the home 4090, so a long cold prompt
          takes a while; repeats reuse the prefix cache.
        </p>
      {/if}
      {#each active.messages as m, i (i)}
        {#if m.role === "user"}
          <div class="msg user">{m.content}</div>
        {:else}
          {@const st = m.turn ? turnStats(m.turn) : null}
          <div class="msg bot">
            {#if m.turn?.reasoning}
              <details
                class="reasoning"
                open={streaming &&
                  i === active.messages.length - 1 &&
                  !m.turn.content}
              >
                <summary
                  >thinking{st?.thinkingMs != null
                    ? ` · ${formatMs(st.thinkingMs)}`
                    : ""}</summary
                >
                <pre>{m.turn.reasoning}</pre>
              </details>
            {/if}
            {#if m.content}
              <div class="md">{@html render(m.content)}</div>
            {:else if streaming && i === active.messages.length - 1}
              <p class="pending">
                {m.turn?.firstTokenAt ? "thinking…" : "prefilling…"}
              </p>
            {/if}
            {#if m.turn?.error}
              <p class="err">{m.turn.error}</p>
            {/if}
            {#if st && m.turn.usage}
              <p class="meta">
                TTFT {formatMs(st.ttftMs)} · decode {formatRate(st.decodeTps)} ·
                {formatCount(st.completionTokens)} tokens · cache {pct(
                  st.cacheHitRate,
                )}
              </p>
            {/if}
          </div>
        {/if}
      {/each}
    </div>

    <form
      class="composer"
      onsubmit={(e) => {
        e.preventDefault();
        send();
      }}
    >
      <textarea
        bind:value={input}
        onkeydown={onKey}
        rows="3"
        placeholder="Message Qwen (Enter to send, Shift+Enter for a new line)"
      ></textarea>
      {#if streaming}
        <button type="button" class="stop" onclick={stop}>Stop</button>
      {:else}
        <button type="submit" disabled={!input.trim()}>Send</button>
      {/if}
    </form>
  </section>

  <aside class="stats" aria-label="Inference stats">
    <section>
      <p class="sec-label">/ Last turn</p>
      <dl>
        <dt>TTFT</dt>
        <dd>{formatMs(last?.ttftMs)}</dd>
        <dt>Thinking</dt>
        <dd>{formatMs(last?.thinkingMs)}</dd>
        <dt>Prefill</dt>
        <dd>{formatRate(last?.prefillTps)}</dd>
        <dt>Decode</dt>
        <dd>{formatRate(last?.decodeTps)}</dd>
        <dt>Prompt</dt>
        <dd>{formatCount(last?.promptTokens)}</dd>
        <dt>Cached</dt>
        <dd>{formatCount(last?.cachedTokens)} ({pct(last?.cacheHitRate)})</dd>
        <dt>Completion</dt>
        <dd>{formatCount(last?.completionTokens)}</dd>
        <dt>Wall</dt>
        <dd>{formatMs(last?.wallMs)}</dd>
      </dl>
    </section>

    <section>
      <p class="sec-label">/ Session</p>
      <dl>
        <dt>Turns</dt>
        <dd>{session.turns}</dd>
        <dt>Mean TTFT</dt>
        <dd>{formatMs(session.meanTtftMs)}</dd>
        <dt>Mean decode</dt>
        <dd>{formatRate(session.meanDecodeTps)}</dd>
        <dt>Tokens in / out</dt>
        <dd>
          {formatCount(session.promptTokens)} / {formatCount(
            session.completionTokens,
          )}
        </dd>
        <dt>Prefix cache</dt>
        <dd>{pct(session.cacheHitRate)}</dd>
        <dt>Generating</dt>
        <dd>{formatMs(session.generationMs)}</dd>
      </dl>
      <p class="gauge-label">
        context {formatCount(session.contextTokens)} / {formatCount(ctxLimit)}
      </p>
      <div class="gauge">
        <span
          style="width: {Math.min(
            100,
            (session.contextTokens / ctxLimit) * 100,
          )}%"
        ></span>
      </div>
      {#if turnSeries.length > 1}
        <p class="gauge-label">decode tok/s per turn</p>
        <svg
          class="spark"
          viewBox="0 0 220 44"
          role="img"
          aria-label="Decode rate per turn"
        >
          <polyline points={sparkline(turnSeries.map((s) => s.decodeTps))} />
        </svg>
        <p class="gauge-label">TTFT per turn</p>
        <svg
          class="spark"
          viewBox="0 0 220 44"
          role="img"
          aria-label="Time to first token per turn"
        >
          <polyline points={sparkline(turnSeries.map((s) => s.ttftMs))} />
        </svg>
      {/if}
    </section>

    <section>
      <p class="sec-label">/ Server (node-4)</p>
      {#if serverError}
        <p class="err">{serverError}</p>
      {:else if server}
        {@const s = server.stats}
        <dl>
          <dt>KV pages</dt>
          <dd>
            {formatCount(s.kv?.used_pages)} / {formatCount(s.kv?.total_pages)}
          </dd>
          <dt>VRAM</dt>
          <dd>
            {formatBytes(s.vram_bytes)} / {formatBytes(
              s.gpus?.[0]?.total_bytes,
            )}
          </dd>
          <dt>Decode now</dt>
          <dd>{formatRate(s.throughput?.decode_tps)}</dd>
          <dt>Prefill now</dt>
          <dd>{formatRate(s.throughput?.prefill_tps)}</dd>
          <dt>Active / done</dt>
          <dd>
            {s.requests?.active ?? "–"} / {formatCount(s.requests?.completed)}
          </dd>
          <dt>Uptime</dt>
          <dd>{formatMs((s.uptime_s ?? 0) * 1000)}</dd>
        </dl>
        {#if server.recent?.length}
          <table class="recent">
            <thead
              ><tr
                ><th>time</th><th>in</th><th>out</th><th>TTFT</th><th>total</th
                ></tr
              ></thead
            >
            <tbody>
              {#each server.recent.slice(-6).reverse() as r}
                <tr>
                  <td>{r.ts?.slice(11, 19)}</td>
                  <td>{formatCount(r.prompt_tokens)}</td>
                  <td>{formatCount(r.completion_tokens)}</td>
                  <td>{formatMs(r.ttft_ms)}</td>
                  <td>{formatMs(r.duration_ms)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      {:else}
        <p class="muted">connecting…</p>
      {/if}
    </section>
  </aside>
</main>

<style>
  :global(body) {
    margin: 0;
  }

  .qwen-page {
    display: grid;
    grid-template-columns: 13rem minmax(0, 1fr) 17rem;
    height: 100dvh;
    background: var(--paper);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 15px;
  }

  .sessions,
  .stats {
    border-color: var(--line);
    border-style: solid;
    border-width: 0;
    overflow-y: auto;
    padding: 1rem;
  }
  .sessions {
    border-right-width: 1px;
  }
  .stats {
    border-left-width: 1px;
    font-size: 13px;
  }

  .sessions ul {
    list-style: none;
    margin: 0.75rem 0 0;
    padding: 0;
  }
  .sessions li {
    display: flex;
    border-radius: 8px;
  }
  .sessions li.current {
    background: var(--surface);
  }
  .pick {
    flex: 1;
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pick,
  .drop {
    background: none;
    border: 0;
    color: var(--ink-2);
    font: inherit;
    padding: 0.4rem 0.5rem;
    cursor: pointer;
  }
  .drop {
    color: var(--ink-3);
  }

  button {
    font: inherit;
    cursor: pointer;
  }
  .new,
  .composer button {
    border: 1px solid var(--line);
    background: var(--card-bg);
    color: var(--ink);
    border-radius: 8px;
    padding: 0.45rem 0.9rem;
  }
  .composer button[type="submit"] {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--card-bg);
  }
  .stop {
    border-color: var(--bad);
    color: var(--bad);
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }

  .chat {
    display: grid;
    grid-template-rows: auto minmax(0, 1fr) auto;
    min-width: 0;
  }
  .bar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 1rem;
    padding: 0.75rem 1.25rem;
    border-bottom: 1px solid var(--line);
  }
  h1 {
    font-family: var(--font-display);
    font-size: 1.3rem;
    margin: 0;
  }
  .home {
    color: var(--ink-3);
    text-decoration: none;
    margin-right: 0.25rem;
  }
  .model {
    font-family: var(--font-code);
    font-size: 0.75rem;
    color: var(--ink-3);
    margin-left: 0.5rem;
  }
  .controls {
    display: flex;
    gap: 1rem;
    color: var(--ink-2);
    font-size: 0.85rem;
  }
  select {
    font: inherit;
    background: var(--card-bg);
    color: var(--ink);
    border: 1px solid var(--line);
    border-radius: 6px;
  }

  .log {
    overflow-y: auto;
    padding: 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }
  .empty,
  .muted,
  .pending {
    color: var(--ink-3);
  }
  .msg {
    max-width: 52rem;
    line-height: 1.55;
  }
  .msg.user {
    align-self: flex-end;
    background: var(--surface);
    border-radius: 12px;
    padding: 0.6rem 0.9rem;
    white-space: pre-wrap;
  }
  .msg.bot {
    align-self: flex-start;
    width: 100%;
  }
  .md :global(pre) {
    background: var(--surface);
    padding: 0.75rem;
    border-radius: 8px;
    overflow-x: auto;
    font-family: var(--font-code);
    font-size: 0.85rem;
  }
  .md :global(code) {
    font-family: var(--font-code);
  }
  .reasoning {
    border-left: 2px solid var(--line);
    padding-left: 0.75rem;
    margin-bottom: 0.5rem;
    color: var(--ink-2);
  }
  .reasoning summary {
    cursor: pointer;
    font-size: 0.8rem;
    color: var(--ink-3);
  }
  .reasoning pre {
    white-space: pre-wrap;
    font-family: var(--font-ui);
    font-size: 0.85rem;
    margin: 0.4rem 0 0;
  }
  .meta {
    font-family: var(--font-code);
    font-size: 0.72rem;
    color: var(--ink-3);
    margin: 0.35rem 0 0;
  }
  .err {
    color: var(--bad);
    font-size: 0.85rem;
  }

  .composer {
    display: flex;
    gap: 0.75rem;
    align-items: flex-end;
    padding: 0.9rem 1.25rem;
    border-top: 1px solid var(--line);
  }
  textarea {
    flex: 1;
    resize: vertical;
    font: inherit;
    padding: 0.6rem 0.75rem;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: var(--card-bg);
    color: var(--ink);
  }

  .sec-label {
    font-family: var(--font-code);
    font-size: 0.72rem;
    color: var(--ink-3);
    margin: 0 0 0.5rem;
  }
  .stats section + section {
    margin-top: 1.25rem;
    padding-top: 1rem;
    border-top: 1px solid var(--line);
  }
  dl {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.75rem;
    margin: 0;
  }
  dt {
    color: var(--ink-2);
  }
  dd {
    margin: 0;
    text-align: right;
    font-family: var(--font-code);
  }
  .gauge-label {
    margin: 0.75rem 0 0.25rem;
    color: var(--ink-3);
    font-size: 0.75rem;
  }
  .gauge {
    height: 6px;
    border-radius: 3px;
    background: var(--surface);
    overflow: hidden;
  }
  .gauge span {
    display: block;
    height: 100%;
    background: var(--accent);
  }
  .spark {
    width: 100%;
    height: 44px;
  }
  .spark polyline {
    fill: none;
    stroke: var(--accent);
    stroke-width: 1.5;
  }
  .recent {
    width: 100%;
    margin-top: 0.75rem;
    border-collapse: collapse;
    font-family: var(--font-code);
    font-size: 0.7rem;
  }
  .recent th {
    color: var(--ink-3);
    font-weight: normal;
    text-align: right;
  }
  .recent td {
    text-align: right;
    padding: 0.1rem 0;
  }
  .recent th:first-child,
  .recent td:first-child {
    text-align: left;
  }

  @media (max-width: 1100px) {
    .qwen-page {
      grid-template-columns: minmax(0, 1fr) 16rem;
    }
    .sessions {
      display: none;
    }
  }
  @media (max-width: 760px) {
    .qwen-page {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: minmax(0, 1fr) auto;
      height: auto;
      min-height: 100dvh;
    }
    .chat {
      height: 100dvh;
    }
    .stats {
      border-left-width: 0;
      border-top-width: 1px;
    }
  }
</style>
