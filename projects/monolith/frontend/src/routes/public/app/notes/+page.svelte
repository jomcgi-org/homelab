<script>
  // The public notes app (ADR 005, V3) lives at /app/notes alongside the other
  // apps. It IS a neo-brutalist chat box wired to my public knowledge graph. A
  // Turnstile challenge is the "start chatting" gate; on solve we open a
  // server-side session (admission.js sets the httpOnly cookie) and reveal the
  // input. Each turn streams from the in-cluster model through the SSR proxy,
  // and the set of public notes the turn grounded on (node_touched) drives the
  // graph view, which highlights and expands those nodes.
  //
  // Chat is the default view. A Chat | Graph toggle switches to the graph view,
  // which is LAZY: the heavy KnowledgeGraph component and its data are only
  // imported/fetched on the first switch, never on the initial chat load.
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import { renderMarkdown } from "$lib/components/notes/markdown.js";
  import {
    streamChatMessage,
    initialTurnState,
    applyFrame,
    CHARACTER_LIMIT,
  } from "$lib/public/chat/stream.js";
  import { freshChatState } from "$lib/public/chat/chat-state.js";

  let { data } = $props();

  // ── view toggle ──────────────────────────────────────────────────
  // "chat" (default) | "graph". The graph component is dynamically imported
  // the first time the visitor opens it, so neither the canvas/d3 code nor the
  // graph payload load on the initial chat render.
  let view = $state("chat");
  /** @type {any} */
  let GraphView = $state(null);
  let graphLoadError = $state(false);
  let graphFocusId = $state(null);

  async function ensureGraphView() {
    if (GraphView || graphLoadError) return;
    try {
      const mod = await import("$lib/public/chat/GraphView.svelte");
      GraphView = mod.default;
    } catch {
      graphLoadError = true;
    }
  }

  function showGraph(focusId = null) {
    graphFocusId = focusId;
    view = "graph";
    ensureGraphView();
  }

  function showChat() {
    view = "chat";
  }

  // ── admission / session ──────────────────────────────────────────
  let admitted = $state(false);

  // ── transcript ───────────────────────────────────────────────────
  // Committed turns. Each: { role: "user" | "assistant", content, touched? }.
  let messages = $state([]);
  let input = $state("");
  let sending = $state(false);
  // The in-flight assistant turn (token stream + per-turn touched set).
  let turn = $state(initialTurnState());
  // A soft "busy" / hard "error" notice for the last turn, with a retry handle.
  let notice = $state(null);
  let lastUserMessage = $state("");
  let inputEl = $state();
  let transcriptEl = $state();
  let controller = null;

  // ── grounding ────────────────────────────────────────────────────
  // The cumulative set of public notes the whole conversation has touched,
  // deduped by id and in first-seen order. Drives the graph highlight.
  let touchedMap = $state(new Map());
  let touched = $derived([...touchedMap.values()]);

  // Starters that map to Joe's dense public notes, so they retrieve and ground
  // well (TSA / Alert Fatigue / Hexagonal Architecture / Production Readiness
  // Review notes).
  const EXAMPLES = [
    "What is the TSA method?",
    "How do you think about alert fatigue?",
    "Explain hexagonal architecture",
    "What is a production readiness review?",
  ];

  const noticeIsBusy = $derived(notice?.kind === "busy");

  function autoScroll() {
    // Keep the latest tokens in view as they stream in.
    if (transcriptEl) transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  $effect(() => {
    // Re-run whenever the transcript or the streaming reply grows.
    messages.length;
    turn.assistant;
    autoScroll();
  });

  function renderReply(text) {
    // Model output is untrusted (ADR 005 layer 8). renderMarkdown HTML-escapes
    // &<> on every path and emits no raw HTML, links, or javascript:/data:
    // URLs, so injected markup renders as inert text and never reaches the DOM
    // as live nodes (covered by markdown.test.js XSS cases). The app sets no
    // CSP (deferred to a later hardening pass), so this escaping is the
    // protection that matters. An empty title map means [[wikilinks]] render as
    // inert text, which is correct: navigation happens through the graph view,
    // not the reply body.
    return renderMarkdown(text ?? "", new Map());
  }

  function mergeTouched(frame) {
    const id = frame.data?.id;
    if (id === undefined || id === null || touchedMap.has(id)) return;
    const next = new Map(touchedMap);
    next.set(id, { id, title: frame.data?.title ?? "" });
    touchedMap = next;
  }

  async function send(text) {
    const trimmed = (text ?? "").trim();
    if (!trimmed || sending) return;
    if (trimmed.length > CHARACTER_LIMIT) return;

    notice = null;
    lastUserMessage = trimmed;
    messages = [...messages, { role: "user", content: trimmed }];
    input = "";
    sending = true;
    turn = initialTurnState();
    controller = new AbortController();

    try {
      await streamChatMessage(trimmed, {
        signal: controller.signal,
        onFrame: (frame) => {
          turn = applyFrame(turn, frame);
          if (frame?.type === "node_touched") mergeTouched(frame);
        },
      });
    } catch {
      turn = { ...turn, status: "error", error: "The connection dropped. Please try again." };
    }

    // Commit any streamed reply, then surface a notice for busy / error so the
    // user can retry the same message.
    if (turn.assistant) {
      messages = [
        ...messages,
        { role: "assistant", content: turn.assistant, touched: turn.touched },
      ];
    }
    if (turn.status === "busy" || turn.status === "error") {
      notice = { kind: turn.status, message: turn.error };
    }
    turn = initialTurnState();
    sending = false;
    controller = null;
    queueMicrotask(() => inputEl?.focus());
  }

  function applyFreshState() {
    // Reset every piece of conversation state from the single source of truth
    // (freshChatState) so the reset and the initial render can never drift.
    const s = freshChatState();
    messages = s.messages;
    touchedMap = s.touchedMap;
    turn = s.turn;
    notice = s.notice;
    input = s.input;
    lastUserMessage = s.lastUserMessage;
  }

  function newChat() {
    // Abort any in-flight turn, clear the transcript + grounding set, and
    // re-open the admission gate. The server session can only be reset by
    // creating a new one, and the session-create flow requires a fresh
    // Turnstile solve, so dropping back to the gate is what starts a truly
    // fresh server session (the new session row supersedes the old cookie).
    controller?.abort();
    controller = null;
    sending = false;
    applyFreshState();
    admitted = false;
    view = "chat";
  }

  function onSubmit(e) {
    e.preventDefault();
    send(input);
  }

  function onKeydown(e) {
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(input);
    }
  }

  $effect(() => {
    return () => controller?.abort();
  });
</script>

<svelte:head>
  <title>Notes · jomcgi.dev</title>
  <meta
    name="description"
    content="A neo-brutalist chat box wired to my public knowledge graph. Ask a question and switch to the graph to deep-dive into the notes the conversation touched."
  />
</svelte:head>

<!-- Visually hidden heading: the full-screen app has no big serif title (the
     PUBLIC CHAT bar is the in-app header), but keep a real h1 for SEO + a11y. -->
<h1 class="sr-only">Chat with my knowledge graph</h1>

<main class="chat-app">
  <!-- Slim explainer banner pinned to the TOP (coral, inviting, collapsible):
       the security posture (open model, no tools, public notes) up front rather
       than buried at the bottom. -->
  <details class="explainer">
    <summary class="explainer-summary">
      <span class="explainer-eyebrow">HOW DOES THIS WORK?</span>
      <span class="explainer-hint">an open model on my own cluster, no tools</span>
      <span class="explainer-mark" aria-hidden="true">+</span>
    </summary>
    <div class="explainer-body">
      <p>
        An open model running on my own cluster answers your questions. It is
        grounded only on my public notes, pulled in by retrieval over the
        knowledge graph, so it talks about what I have actually written.
      </p>
      <p>
        It has no tools and cannot act: it only reads those notes and writes
        text back into this chat. Replies can be wrong, and every note it reads
        is public by design.
      </p>
    </div>
  </details>

  <div class="app-toolbar">
    <div class="view-toggle" role="tablist" aria-label="Notes view">
      <button
        type="button"
        class="view-toggle-btn"
        class:on={view === "chat"}
        role="tab"
        aria-selected={view === "chat"}
        onclick={showChat}
      >
        CHAT
      </button>
      <button
        type="button"
        class="view-toggle-btn"
        class:on={view === "graph"}
        role="tab"
        aria-selected={view === "graph"}
        onclick={() => showGraph(null)}
      >
        GRAPH
      </button>
    </div>
  </div>

  <div class="view-area">
    {#if view === "chat"}
    <section class="chat-box">
      <div class="chat-box-bar">
        <span class="chat-box-tag">PUBLIC CHAT</span>
        <span class="chat-box-status" class:on={admitted}>
          {admitted ? "SESSION OPEN" : "LOCKED"}
        </span>
        {#if admitted}
          <div class="chat-box-actions">
            <button
              type="button"
              class="bar-btn"
              onclick={() => showGraph(null)}
            >
              DEEP DIVE
            </button>
            <button type="button" class="bar-btn" onclick={newChat}>
              NEW CHAT
            </button>
          </div>
        {/if}
      </div>

      <div class="chat-transcript" bind:this={transcriptEl}>
        {#if !admitted}
          <div class="chat-gate">
            <p class="chat-gate-eyebrow">START CHATTING</p>
            <p class="chat-gate-copy">
              Solve the challenge once to open a session. No sign-in, no
              tracking beyond what keeps the bots out.
            </p>
            <TurnstileGate
              siteKey={data.turnstileSiteKey}
              onAdmitted={() => {
                admitted = true;
                queueMicrotask(() => inputEl?.focus());
              }}
            />
          </div>
        {:else if messages.length === 0 && !sending}
          <div class="chat-empty">
            <p class="chat-empty-copy">
              Ask anything about my notes, projects, or how this homelab is
              built.
            </p>
            <div class="chat-examples">
              {#each EXAMPLES as ex}
                <button
                  type="button"
                  class="chat-example"
                  onclick={() => send(ex)}
                >
                  {ex}
                </button>
              {/each}
            </div>
          </div>
        {:else}
          {#each messages as m}
            {#if m.role === "user"}
              <article class="turn turn-user">
                <p class="turn-tag">YOU</p>
                <p class="turn-user-text">{m.content}</p>
              </article>
            {:else}
              <article class="turn turn-graph">
                <p class="turn-tag">GRAPH</p>
                <div class="turn-md">{@html renderReply(m.content)}</div>
                {#if m.touched && m.touched.length}
                  <div class="turn-touched">
                    <span class="turn-touched-label">GROUNDED IN</span>
                    {#each m.touched as n}
                      <button
                        type="button"
                        class="touched-chip"
                        onclick={() => showGraph(n.id)}
                      >
                        {n.title || "untitled note"}
                      </button>
                    {/each}
                  </div>
                {/if}
              </article>
            {/if}
          {/each}

          {#if sending}
            <article class="turn turn-graph">
              <p class="turn-tag">GRAPH</p>
              {#if turn.assistant}
                <div class="turn-md">{@html renderReply(turn.assistant)}<span class="caret"></span></div>
              {:else}
                <p class="turn-thinking">
                  <span class="dot"></span><span class="dot"></span><span
                    class="dot"
                  ></span>
                  {touched.length ? "reading the graph" : "thinking"}
                </p>
              {/if}
            </article>
          {/if}
        {/if}
      </div>

      {#if notice}
        <div class="chat-notice" class:busy={noticeIsBusy} role="status">
          <span class="chat-notice-text">{notice.message}</span>
          <button
            type="button"
            class="chat-notice-retry"
            onclick={() => send(lastUserMessage)}
          >
            TRY AGAIN
          </button>
        </div>
      {/if}

      {#if admitted}
        <form class="chat-input" onsubmit={onSubmit}>
          <textarea
            bind:this={inputEl}
            bind:value={input}
            onkeydown={onKeydown}
            placeholder="Ask the graph..."
            rows="1"
            maxlength={CHARACTER_LIMIT}
            disabled={sending}
            aria-label="Your message"
          ></textarea>
          <div class="chat-input-side">
            <span class="chat-count" class:warn={input.length > CHARACTER_LIMIT * 0.9}>
              {input.length}/{CHARACTER_LIMIT}
            </span>
            <button
              type="submit"
              class="chat-send"
              disabled={sending || !input.trim()}
            >
              {sending ? "..." : "SEND"}
            </button>
          </div>
        </form>
      {/if}
    </section>
  {:else if graphLoadError}
    <section class="graph-fallback">
      <p class="graph-fallback-copy">The graph could not be loaded.</p>
      <button
        type="button"
        class="graph-fallback-retry"
        onclick={() => {
          graphLoadError = false;
          ensureGraphView();
        }}
      >
        TRY AGAIN
      </button>
    </section>
  {:else if GraphView}
    <GraphView {touched} focusId={graphFocusId} />
  {:else}
    <section class="graph-fallback">
      <p class="graph-thinking">
        <span class="dot"></span><span class="dot"></span><span class="dot"
        ></span>
        opening the graph
      </p>
    </section>
  {/if}
  </div>
</main>

<style>
  /* ── full-screen app shell ──────────────────────────────────── */
  /* No site nav on /app/* routes (same convention as /app/ships): this page
     owns the whole viewport. A flex column with generous gaps so the explainer,
     toolbar, and the chat/graph surface sit apart and breathe. 100dvh tracks
     the dynamic mobile viewport; the safe-area insets keep it off the notch and
     the home indicator. */
  .chat-app {
    box-sizing: border-box;
    height: 100vh;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    gap: 18px;
    padding: calc(24px + env(safe-area-inset-top)) calc(24px + env(safe-area-inset-right))
      calc(24px + env(safe-area-inset-bottom)) calc(24px + env(safe-area-inset-left));
    overflow: hidden;
    font-family: var(--mono);
    color: var(--ink);
    background: var(--bg);
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  /* ── top explainer banner (coral, inviting, collapsible) ─────── */
  .explainer {
    flex: none;
    border: 2px solid var(--ink);
    background: var(--coral);
    box-shadow: var(--shadow-hard-sm);
  }
  .explainer-summary {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px 16px;
    cursor: pointer;
    list-style: none;
  }
  .explainer-summary::-webkit-details-marker {
    display: none;
  }
  .explainer-eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
  }
  .explainer-hint {
    font-size: 11px;
    color: var(--ink-2);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .explainer-mark {
    margin-left: auto;
    font-size: 18px;
    line-height: 1;
    transition: transform 150ms ease;
  }
  .explainer[open] .explainer-mark {
    transform: rotate(45deg);
  }
  .explainer-body {
    padding: 4px 16px 16px;
    background: var(--paper);
    border-top: 2px solid var(--ink);
  }
  .explainer-body p {
    font-size: 12px;
    line-height: 1.6;
    color: var(--ink-2);
    max-width: 72ch;
    margin: 12px 0 0;
  }

  /* ── toolbar ────────────────────────────────────────────────── */
  .app-toolbar {
    flex: none;
    display: flex;
    align-items: center;
  }

  /* ── view + chat surface fill the rest of the viewport ──────── */
  .view-area {
    flex: 1;
    min-height: 0;
    display: flex;
  }
  .view-area > :global(.graph-view) {
    flex: 1;
    min-height: 0;
  }

  /* ── view toggle ────────────────────────────────────────────── */
  /* Two separate brutalist boxes (not a segmented control) so the inactive
     tab can lift on hover the way every other box on the site does. */
  .view-toggle {
    display: inline-flex;
    gap: 12px;
  }
  .view-toggle-btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
    padding: 9px 20px;
    border: 2px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
    box-shadow: var(--shadow-hard-sm);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease,
      background 120ms ease;
  }
  .view-toggle-btn.on {
    background: var(--accent);
  }
  .view-toggle-btn:hover:not(.on) {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
  }
  .view-toggle-btn:active:not(.on) {
    transform: none;
    box-shadow: var(--shadow-hard-sm);
  }

  /* ── chat box shell ─────────────────────────────────────────── */
  /* Fills the view-area (flex:1) so the chat surface is the full-screen app;
     the transcript scrolls internally and the input stays pinned. */
  .chat-box {
    flex: 1;
    min-height: 0;
    width: 100%;
    border: 2px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard-lg);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  /* The in-app header bar (PUBLIC CHAT / status / actions). Kept neutral paper
     so yellow is not the dominant colour; the PUBLIC CHAT tag carries a blue
     chip and the status carries green for variety. */
  .chat-box-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 18px;
    background: var(--paper);
    border-bottom: 2px solid var(--ink);
  }
  .chat-box-tag {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    padding: 3px 9px;
    border: 1.5px solid var(--ink);
    background: var(--blue);
  }
  .chat-box-status {
    font-size: 10px;
    letter-spacing: 0.1em;
    padding: 2px 7px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
  }
  .chat-box-status.on {
    background: var(--green);
  }
  .chat-box-actions {
    display: flex;
    gap: 8px;
    margin-left: auto;
  }
  .bar-btn {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 5px 11px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .bar-btn:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard-sm);
  }
  .bar-btn:active {
    transform: none;
    box-shadow: none;
  }

  /* ── transcript ─────────────────────────────────────────────── */
  /* Tall and generous: the transcript fills most of the viewport and scrolls,
     while the input row below stays pinned to the bottom of the box. */
  .chat-transcript {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 18px;
    background: var(--bg-elev);
  }

  /* gate + empty */
  .chat-gate,
  .chat-empty {
    margin: auto 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  .chat-gate-eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.16em;
  }
  .chat-gate-copy,
  .chat-empty-copy {
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink-2);
    max-width: 54ch;
  }
  .chat-examples {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
  }
  .chat-example {
    font-family: var(--mono);
    font-size: 12px;
    padding: 9px 13px;
    border: 1.5px solid var(--ink);
    background: var(--paper);
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease,
      background 120ms ease;
  }
  .chat-example:hover {
    background: var(--accent);
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard-sm);
  }

  /* turns */
  .turn {
    border: 1.5px solid var(--ink);
    background: var(--paper);
    padding: 12px 14px;
    border-left-width: 5px;
  }
  .turn-user {
    border-left-color: var(--blue);
    align-self: flex-end;
    max-width: 88%;
  }
  .turn-graph {
    border-left-color: var(--accent);
    align-self: flex-start;
    max-width: 100%;
    width: 100%;
  }
  .turn-tag {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.16em;
    color: var(--ink-3);
    margin-bottom: 6px;
  }
  .turn-user-text {
    font-size: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .turn-md {
    font-size: 13px;
    line-height: 1.6;
    word-break: break-word;
  }
  .turn-md :global(p) {
    margin: 0 0 9px;
  }
  .turn-md :global(p:last-child) {
    margin-bottom: 0;
  }
  .turn-md :global(ul) {
    margin: 0 0 9px;
    padding-left: 18px;
    list-style: disc;
  }
  .turn-md :global(ol) {
    margin: 0 0 9px;
    padding-left: 20px;
    list-style: decimal;
  }
  .turn-md :global(li) {
    margin-bottom: 3px;
  }
  .turn-md :global(code) {
    background: var(--bg-elev);
    padding: 1px 5px;
    border: 1px solid var(--rule-2);
    font-size: 12px;
  }
  .turn-md :global(pre) {
    background: var(--bg-elev);
    border: 1px solid var(--ink);
    padding: 10px 12px;
    margin: 9px 0;
    overflow-x: auto;
    font-size: 11.5px;
    white-space: pre;
  }
  .turn-md :global(pre code) {
    background: transparent;
    border: none;
    padding: 0;
  }
  .turn-md :global(strong) {
    font-weight: 700;
  }
  .turn-md :global(blockquote) {
    margin: 9px 0;
    padding: 6px 12px;
    border-left: 3px solid var(--ink);
    background: var(--bg-elev);
  }
  .turn-md :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 9px 0;
    font-size: 11.5px;
  }
  .turn-md :global(th),
  .turn-md :global(td) {
    border: 1px solid var(--ink);
    padding: 4px 8px;
    text-align: left;
  }
  .turn-md :global(th) {
    background: var(--bg-elev);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.06em;
  }

  .caret {
    display: inline-block;
    width: 8px;
    height: 1.05em;
    margin-left: 2px;
    vertical-align: text-bottom;
    background: var(--ink);
    animation: caret-blink 900ms steps(2, start) infinite;
  }
  @keyframes caret-blink {
    50% {
      opacity: 0;
    }
  }

  .turn-thinking,
  .graph-thinking {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--ink-3);
  }
  .turn-thinking .dot,
  .graph-thinking .dot {
    width: 5px;
    height: 5px;
    background: var(--ink-3);
    display: inline-block;
    animation: dot-pulse 1s ease-in-out infinite;
  }
  .turn-thinking .dot:nth-child(2),
  .graph-thinking .dot:nth-child(2) {
    animation-delay: 0.15s;
  }
  .turn-thinking .dot:nth-child(3),
  .graph-thinking .dot:nth-child(3) {
    animation-delay: 0.3s;
  }
  @keyframes dot-pulse {
    0%,
    100% {
      opacity: 0.3;
    }
    50% {
      opacity: 1;
    }
  }

  /* grounded chips */
  .turn-touched {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-top: 11px;
    padding-top: 10px;
    border-top: 1.5px dashed var(--ink);
  }
  .turn-touched-label {
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--ink-3);
  }
  .touched-chip {
    font-family: var(--mono);
    font-size: 11px;
    padding: 3px 8px;
    border: 1.5px solid var(--ink);
    background: var(--blue);
    cursor: pointer;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .touched-chip:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard-sm);
  }

  /* ── notice ─────────────────────────────────────────────────── */
  .chat-notice {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-top: 2px solid var(--ink);
    background: var(--coral);
    font-size: 12px;
  }
  .chat-notice.busy {
    background: var(--accent);
  }
  .chat-notice-text {
    flex: 1;
  }
  .chat-notice-retry {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 5px 10px;
    border: 2px solid var(--ink);
    background: var(--paper);
    cursor: pointer;
  }

  /* ── input ──────────────────────────────────────────────────── */
  .chat-input {
    display: flex;
    gap: 10px;
    align-items: flex-end;
    padding: 12px 14px;
    border-top: 2px solid var(--ink);
    background: var(--paper);
  }
  .chat-input textarea {
    flex: 1;
    resize: none;
    min-height: 44px;
    max-height: 160px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.5;
    padding: 11px 12px;
    border: 2px solid var(--rule-2);
    border-radius: 0;
    background: var(--paper);
    color: var(--ink);
    transition:
      border-color 120ms ease,
      box-shadow 120ms ease;
  }
  /* Single, crisp brutalist focus: the border goes solid ink and the hard
     offset shadow appears. Override the global :focus-visible outline so we do
     not get the doubled / offset blue ring on top of the border. */
  .chat-input textarea:focus,
  .chat-input textarea:focus-visible {
    outline: none;
    border-color: var(--ink);
    box-shadow: var(--shadow-hard-sm);
  }
  .chat-input textarea:disabled {
    opacity: 0.6;
  }
  .chat-input-side {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 6px;
  }
  .chat-count {
    font-size: 9px;
    color: var(--ink-3);
    letter-spacing: 0.04em;
  }
  .chat-count.warn {
    color: var(--coral);
    font-weight: 700;
  }
  .chat-send {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    min-height: 44px;
    padding: 0 20px;
    border: 2px solid var(--ink);
    background: var(--blue);
    color: var(--ink);
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .chat-send:hover:not(:disabled) {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
  }
  .chat-send:active:not(:disabled) {
    transform: none;
    box-shadow: none;
  }
  .chat-send:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* ── graph fallback (loading / error before the lazy view) ──── */
  .graph-fallback {
    flex: 1;
    min-height: 0;
    width: 100%;
    border: 2px solid var(--ink);
    background: var(--bg);
    box-shadow: var(--shadow-hard-lg);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
  }
  .graph-fallback-copy {
    font-size: 13px;
    color: var(--ink-2);
  }
  .graph-fallback-retry {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 7px 12px;
    border: 2px solid var(--ink);
    background: var(--accent);
    cursor: pointer;
    box-shadow: var(--shadow-hard-sm);
  }

  @media (max-width: 640px) {
    .chat-app {
      gap: 12px;
      padding: calc(14px + env(safe-area-inset-top)) calc(14px + env(safe-area-inset-right))
        calc(14px + env(safe-area-inset-bottom)) calc(14px + env(safe-area-inset-left));
    }
    .explainer-hint {
      display: none;
    }
    .chat-transcript {
      padding: 16px;
      gap: 14px;
    }
    .turn-user {
      max-width: 95%;
    }
    .chat-box-actions {
      gap: 6px;
    }
  }
</style>
