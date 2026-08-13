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
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
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

  // ── URL <-> view/focus sync ──────────────────────────────────────
  // Mirror the view toggle ("chat" default | "graph") and the graph focus node
  // to the URL (?view=graph&focus=<id>), so a graph-focused view is shareable
  // and the back button pops the toggle naturally. Defaults are omitted from the
  // URL (no ?view=chat). The ephemeral chat transcript is NOT in the URL: only
  // the structural view/focus state is. Mirrors the old homepage URL-state pattern
  // ($page + goto with keepFocus/noScroll/replaceState).
  function syncUrl() {
    const url = new URL($page.url);
    if (view === "graph") url.searchParams.set("view", "graph");
    else url.searchParams.delete("view");
    if (view === "graph" && graphFocusId != null) {
      url.searchParams.set("focus", String(graphFocusId));
    } else {
      url.searchParams.delete("focus");
    }
    goto(url, { keepFocus: true, noScroll: true, replaceState: true });
  }

  function showGraph(focusId = null) {
    graphFocusId = focusId;
    view = "graph";
    ensureGraphView();
    syncUrl();
  }

  function showChat() {
    view = "chat";
    graphFocusId = null;
    syncUrl();
  }

  // ── "how does this work?" explainer ──────────────────────────────
  // A controlled <details> popover (bind:open). Native <details> never closes on
  // an outside click or Esc, so while it is open we attach document listeners
  // that close it when the visitor clicks outside the disclosure or hits Escape.
  // The effect only runs while open, so there is no idle global listener.
  let explainerOpen = $state(false);
  let explainerEl = $state();
  $effect(() => {
    if (!explainerOpen) return;
    const onDocClick = (e) => {
      if (explainerEl && !explainerEl.contains(e.target)) explainerOpen = false;
    };
    const onKey = (e) => {
      if (e.key === "Escape") explainerOpen = false;
    };
    document.addEventListener("click", onDocClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("click", onDocClick);
      document.removeEventListener("keydown", onKey);
    };
  });

  // ── admission / session ──────────────────────────────────────────
  // Seeded from the server loader: a live session (a reload, or a chat just
  // forked from a shared snapshot) hydrates as already-admitted with its stored
  // transcript, so the visitor resumes rather than re-passing the gate. A
  // cookieless visit hydrates as { admitted: false, [] } and shows the gate.
  let admitted = $state(data.admitted ?? false);

  // ── transcript ───────────────────────────────────────────────────
  // Committed turns. Each: { role: "user" | "assistant", content, touched? }.
  let messages = $state(data.initialMessages ?? []);
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
  // deduped by id and in first-seen order. Drives the graph highlight. Seeded
  // from a hydrated transcript so a resumed/forked session's graph view lights
  // up the notes the carried-over turns grounded on.
  function seedTouched(msgs) {
    const map = new Map();
    for (const msg of msgs ?? []) {
      for (const n of msg.touched ?? []) {
        const id = n?.id;
        if (id === undefined || id === null || map.has(id)) continue;
        map.set(id, { id, title: n.title ?? "" });
      }
    }
    return map;
  }
  let touchedMap = $state(seedTouched(data.initialMessages));
  let touched = $derived([...touchedMap.values()]);

  // ── live ticker readouts ─────────────────────────────────────────
  // The yellow marquee carries a few live numbers about the running session.
  // CTX is the session token total (the `done` SSE frame's `total_tokens`) over
  // the model's 32K context window. TOK/S is an approximate decode rate computed
  // while a turn streams (chars received / 4 per token, over elapsed seconds),
  // settling to the last turn's rate when idle.
  //
  // GPU UTIL / VRAM come from the cluster stats snapshot (the same payload the
  // homepage renders): SSR seeds `data.stats`, and we poll the same-origin
  // /app/notes/stats proxy so the GPU readout stays live while a visitor sits on
  // the page. No new backend endpoint, and the browser never calls the backend
  // directly. When stats are unavailable the GPU items just drop out.
  const MODEL_NAME = "QWEN3.6-27B"; // CHAT_PUBLIC_MODEL = qwen3.6-27b
  const BOT_LABEL = "QWEN3.6 / LOCAL"; // model family + "runs on my cluster"
  const CONTEXT_WINDOW = 32768; // CHAT_PUBLIC_MODEL_WINDOW_TOKENS
  // Public note count for the KG readout + empty-state copy. The chat view does
  // NOT load the graph payload on initial render (that lazy fetch only happens
  // on the GRAPH toggle, by design), so there is no live count here without an
  // extra round-trip. We use a maintained constant; a lightweight count endpoint
  // is the follow-up that would make this live. Keep this in sync with the
  // public graph node count.
  const PUBLIC_NOTE_COUNT = 4636;

  let sessionTokens = $state(data.initialTokens ?? 0); // running CTX total
  let tokPerSec = $state(0); // last computed decode rate
  let turnStart = 0; // performance.now() at turn start (plain, non-reactive)

  // Live cluster stats (homepage GPU/system snapshot). SSR-seeded, then polled.
  let liveStats = $state(data.stats ?? null);
  onMount(() => {
    // Initialize view/focus from the URL so a shared ?view=graph&focus=<id>
    // link lands directly on that graph-focused view (ssr=false, so $page is
    // browser-only and safe to read here). The chat transcript is never
    // initialized from the URL: it is ephemeral session state.
    const params = $page.url.searchParams;
    if (params.get("view") === "graph") {
      // focus is a string from the URL; the graph treats focusId as opaque
      // (selectionForFocus passes it through), so a shared link lands on the
      // graph and best-effort highlights the focused node.
      const focus = params.get("focus");
      showGraph(focus || null);
    }

    let stopped = false;
    const id = setInterval(async () => {
      try {
        const res = await fetch("/app/notes/stats");
        if (!stopped && res.ok) liveStats = await res.json();
      } catch {
        // keep the last good value; the readout never goes blank on a blip
      }
    }, 20_000);
    return () => {
      stopped = true;
      clearInterval(id);
    };
  });

  // GPU readouts derived from the snapshot: utilization and VRAM used / total.
  // Each item appears only when its field is present, so a partial snapshot
  // (e.g. util known but frame buffer unavailable) still surfaces what it can.
  const gpuItems = $derived.by(() => {
    const g = liveStats?.gpu;
    if (!g) return [];
    const items = [];
    if (typeof g.utilization_pct === "number") {
      items.push(`GPU: ${Math.round(g.utilization_pct)}% UTIL`);
    }
    if (
      typeof g.memory_used_gb === "number" &&
      typeof g.memory_total_gb === "number"
    ) {
      items.push(
        `VRAM: ${g.memory_used_gb.toFixed(0)} / ${g.memory_total_gb.toFixed(0)} GB`,
      );
    }
    return items;
  });

  function fmtCount(n) {
    // 1234 -> "1,234". Used for the KG note count and empty-state copy.
    return n.toLocaleString("en-US");
  }
  function fmtTokens(n) {
    // 1180 -> "1.2K", 640 -> "640". Compact CTX readout.
    return n >= 1000 ? `${(n / 1000).toFixed(1)}K` : String(n);
  }

  const tickerItems = $derived([
    `MODEL: ${MODEL_NAME}`,
    ...gpuItems,
    `CTX: ${fmtTokens(sessionTokens)} / ${CONTEXT_WINDOW / 1024}K`,
    `TOK/S: ${tokPerSec.toFixed(1)}`,
    `NOTES: ${fmtCount(PUBLIC_NOTE_COUNT)}`,
    "NO TOOLS / NO CLOUD / NO TELEMETRY",
  ]);

  // Starters that map to Joe's dense public notes, so they retrieve and ground
  // well (TSA / Alert Fatigue / Hexagonal Architecture / Production Readiness
  // Review notes).
  const EXAMPLES = [
    "Where is this model running?",
    "What is STPA?",
    "Why use Bazel in a homelab?",
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
    turnStart = performance.now();

    try {
      await streamChatMessage(trimmed, {
        signal: controller.signal,
        onFrame: (frame) => {
          turn = applyFrame(turn, frame);
          if (frame?.type === "node_touched") mergeTouched(frame);
          if (frame?.type === "token") {
            // Approximate decode rate while streaming: ~4 chars per token over
            // elapsed seconds. Live-ish; settles to the last value when idle.
            const elapsed = (performance.now() - turnStart) / 1000;
            if (elapsed > 0.05) {
              tokPerSec = turn.assistant.length / 4 / elapsed;
            }
          }
        },
      });
    } catch {
      turn = {
        ...turn,
        status: "error",
        error: "The connection dropped. Please try again.",
      };
    }

    // Commit any streamed reply, then surface a notice for busy / error so the
    // user can retry the same message.
    if (turn.assistant) {
      messages = [
        ...messages,
        { role: "assistant", content: turn.assistant, touched: turn.touched },
      ];
    }
    // The `done` frame carries the session's running token total; surface it as
    // the CTX readout in the ticker.
    if (turn.totalTokens) sessionTokens = turn.totalTokens;
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
    // Reset the live ticker readouts too: a new session starts at 0 context.
    sessionTokens = 0;
    tokPerSec = 0;
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
    showChat();
  }

  // ── share this chat ──────────────────────────────────────────────
  // Opt-in, read-only share. POSTs to the same-origin /chat/share proxy (the
  // session id rides the httpOnly cookie, never the body), gets {snapshot_id},
  // and copies the absolute share URL to the clipboard. The snapshot is minted
  // server-side from the stored transcript, so nothing here can forge content.
  let shareFeedback = $state(null); // brief inline status, then clears
  let shareTimer = null;
  let sharing = $state(false);

  function flashShare(message) {
    shareFeedback = message;
    clearTimeout(shareTimer);
    shareTimer = setTimeout(() => {
      shareFeedback = null;
    }, 4000);
  }

  async function shareChat() {
    if (sharing) return;
    sharing = true;
    try {
      const resp = await fetch("/chat/share", { method: "POST" });
      if (!resp.ok) {
        flashShare("COULD NOT SHARE");
        return;
      }
      const { snapshot_id: snapshotId } = await resp.json();
      const shareUrl = `${location.origin}/app/notes/s/${snapshotId}`;
      try {
        await navigator.clipboard.writeText(shareUrl);
        flashShare("LINK COPIED");
      } catch {
        // Clipboard blocked (no permission / insecure context): surface the URL
        // so the visitor can copy it by hand.
        flashShare(shareUrl);
      }
    } catch {
      flashShare("COULD NOT SHARE");
    } finally {
      sharing = false;
    }
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
  <!-- Top header row: the back-to-home breadcrumb on the left (mirroring the
       other /app/* pages, e.g. /app/stars), and the app controls (CHAT | GRAPH
       toggle + the collapsible "HOW DOES THIS WORK?" explainer) on the right,
       on ONE row. Keeping the controls up here on the otherwise-empty
       breadcrumb line saves a full row of vertical height over a separate
       toolbar row; the controls wrap below the crumb on narrow screens. The
       explainer is sized to its own text; expanding it opens a content-width
       popover that overlays downward so it never shoves the layout. -->
  <header class="app-header">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
        ></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">notes</span>
    </nav>

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

      <details
        class="explainer"
        bind:open={explainerOpen}
        bind:this={explainerEl}
      >
        <summary class="explainer-summary">
          <span class="explainer-mark" aria-hidden="true">+</span>
          <span class="explainer-eyebrow">HOW DOES THIS WORK?</span>
          <span class="explainer-hint"
            >An open model on my own cluster, no tools.</span
          >
        </summary>
        <div class="explainer-body">
          <p>
            Agents research topics and write them into my knowledge graph. This
            model answers from that graph, nothing else. Answers can be wrong.
          </p>
        </div>
      </details>
    </div>
  </header>

  <!-- Live ticker: a scrolling yellow marquee of session readouts (model, live
       CTX usage / 32K, live decode rate, KG size, posture). The run is tripled
       so the loop is seamless; prefers-reduced-motion stops the scroll. -->
  <div class="ticker" aria-hidden="true">
    <div class="ticker-track">
      {#each { length: 3 } as _}
        {#each tickerItems as item}
          <span class="ticker-item"><span class="ticker-dot"></span>{item}</span
          >
        {/each}
      {/each}
    </div>
  </div>

  <div class="view-area">
    {#if view === "chat"}
      <section class="chat-box">
        <div class="panel-head">
          <span class="panel-tag">PUBLIC CHAT</span>
          <span class="session" class:on={admitted}>
            <span class="led" class:on={admitted}></span>
            {admitted ? "SESSION OPEN" : "NOT STARTED"}
          </span>
          <span class="panel-spacer"></span>
          {#if admitted && messages.length > 0}
            {#if shareFeedback}
              <span class="share-feedback" role="status">{shareFeedback}</span>
            {/if}
            <button
              type="button"
              class="bar-btn"
              onclick={shareChat}
              disabled={sharing}
            >
              {sharing ? "..." : "SHARE"}
            </button>
          {/if}
          {#if admitted}
            <button type="button" class="bar-btn" onclick={newChat}>
              NEW CHAT
            </button>
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
              <!-- Scattered brutalist doodles, decorative only. Hidden under 640px
                 and pointer-events:none so they never interfere. -->
              <svg
                class="doodle doodle-cloud"
                viewBox="0 0 64 40"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M14 32 Q4 32 5 24 Q5 16 14 17 Q15 7 26 9 Q33 2 42 10 Q54 9 53 19 Q62 19 60 27 Q59 32 50 32 Z"
                  stroke="currentColor"
                  stroke-width="2.5"
                  stroke-linejoin="round"
                />
              </svg>
              <svg
                class="doodle doodle-star"
                viewBox="0 0 40 40"
                aria-hidden="true"
              >
                <path
                  d="M20 2 L24 16 L38 20 L24 24 L20 38 L16 24 L2 20 L16 16 Z"
                  fill="var(--blue)"
                  stroke="var(--ink)"
                  stroke-width="2"
                  stroke-linejoin="round"
                />
              </svg>
              <svg
                class="doodle doodle-squiggle"
                viewBox="0 0 84 20"
                fill="none"
                aria-hidden="true"
              >
                <path
                  d="M2 10 Q12 -1 22 10 T42 10 T62 10 T82 10"
                  stroke="currentColor"
                  stroke-width="2.5"
                  stroke-linecap="round"
                />
              </svg>
              <svg
                class="doodle doodle-diamond"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <rect
                  x="6"
                  y="6"
                  width="12"
                  height="12"
                  transform="rotate(45 12 12)"
                  fill="var(--coral)"
                  stroke="var(--ink)"
                  stroke-width="2"
                />
              </svg>

              <h2 class="empty-headline">
                ask my notes <span class="empty-hl">anything.</span>
              </h2>
              <p class="empty-sub">
                There are {fmtCount(PUBLIC_NOTE_COUNT)} of them: coffee logs, design
                notes, dark-sky readings, half-finished side quests. A model running
                on my own machines reads them and answers. Your questions never go
                to a model vendor.
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
                  <div class="user-bubble">{m.content}</div>
                </article>
              {:else}
                <article class="turn turn-bot">
                  <p class="bot-label">{BOT_LABEL}</p>
                  <div class="turn-md">{@html renderReply(m.content)}</div>
                  {#if m.touched && m.touched.length}
                    <div class="turn-touched">
                      <span class="turn-touched-label">BASED ON</span>
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
              <article class="turn turn-bot">
                <p class="bot-label">{BOT_LABEL}</p>
                {#if turn.assistant}
                  <div class="turn-md">
                    {@html renderReply(turn.assistant)}<span class="caret"
                    ></span>
                  </div>
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
            <!-- Small blue squiggle doodle anchored bottom-left of the dock. -->
            <svg
              class="dock-doodle"
              viewBox="0 0 60 14"
              fill="none"
              aria-hidden="true"
            >
              <path
                d="M2 7 Q9 0 16 7 T30 7 T44 7 T58 7"
                stroke="var(--blue)"
                stroke-width="2.5"
                stroke-linecap="round"
              />
            </svg>
            <textarea
              bind:this={inputEl}
              bind:value={input}
              onkeydown={onKeydown}
              placeholder="Ask the graph..."
              rows="1"
              maxlength={CHARACTER_LIMIT}
              disabled={sending}
              aria-label="Your message"></textarea>
            <div class="chat-input-side">
              <span
                class="chat-count"
                class:warn={input.length > CHARACTER_LIMIT * 0.9}
              >
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
     owns the whole viewport. A flex column with generous gaps so the ticker,
     toolbar, and the chat/graph surface sit apart and breathe. 100dvh tracks
     the dynamic mobile viewport; the safe-area insets keep it off the notch and
     the home indicator. */
  .chat-app {
    box-sizing: border-box;
    height: 100vh;
    height: 100dvh;
    display: flex;
    flex-direction: column;
    gap: 14px;
    padding: calc(20px + env(safe-area-inset-top))
      calc(24px + env(safe-area-inset-right))
      calc(20px + env(safe-area-inset-bottom))
      calc(24px + env(safe-area-inset-left));
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

  /* ── breadcrumb (back-to-home, matches /app/stars) ───────────── */
  .crumb {
    flex: none;
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .crumb-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-decoration-skip-ink: none;
    text-underline-offset: 2px;
    padding: 0 2px;
    transition: background 140ms ease;
  }
  .crumb-home:hover,
  .crumb-home:focus-visible {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
    text-decoration-color: var(--ink);
  }
  .crumb-arrow {
    font-size: 0.85em;
    margin-left: 1px;
  }
  .crumb-sep {
    color: var(--ink-3);
  }

  /* ── live ticker (yellow scrolling marquee) ─────────────────── */
  /* A thin yellow band of session readouts under the breadcrumb. Tripled run +
     translateX(-33.333%) gives a seamless loop. Coral dots separate segments. */
  .ticker {
    flex: none;
    /* Full-bleed: span the whole viewport width edge to edge, escaping the app
       shell's horizontal padding (incl. safe-area insets). For a flex child the
       50% resolves against the parent's content width, so this margin collapses
       to exactly -padding at every breakpoint. No shadow: just top/bottom rules
       so the band reads as a clean stripe. */
    width: 100vw;
    margin-left: calc(50% - 50vw);
    background: var(--accent);
    color: var(--ink);
    border-top: 2px solid var(--ink);
    border-bottom: 2px solid var(--ink);
    overflow: hidden;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .ticker-track {
    display: flex;
    gap: 36px;
    padding: 7px 0;
    width: max-content;
    animation: ticker-scroll 28s linear infinite;
  }
  .ticker-item {
    display: inline-flex;
    align-items: center;
    gap: 12px;
    white-space: nowrap;
  }
  .ticker-dot {
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: var(--coral);
    border: 1.5px solid var(--ink);
    display: inline-block;
    flex-shrink: 0;
  }
  @keyframes ticker-scroll {
    to {
      transform: translateX(-33.333%);
    }
  }

  /* ── toolbar (view toggle + inline explainer on one row) ─────── */
  /* ── top header row (breadcrumb + controls on one line) ─────── */
  /* The breadcrumb sits left; the CHAT | GRAPH toggle and explainer sit right.
     space-between pins them to the two ends; flex-wrap lets the controls drop
     below the crumb on narrow screens instead of overflowing. */
  .app-header {
    flex: none;
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px 16px;
  }
  .app-toolbar {
    flex: none;
    display: flex;
    align-items: center;
    gap: 16px;
  }

  /* ── inline explainer (neutral disclosure, sized to its text) ── */
  /* Sits to the right of the CHAT | GRAPH toggle. The collapsed summary is just
     wide enough for its text; expanding opens a content-width popover under the
     summary (absolute, so it does not shove the toolbar or the chat surface). */
  .explainer {
    flex: none;
    position: relative;
    border: 2px solid var(--ink);
    background: var(--paper);
  }
  .explainer-summary {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 8px 14px;
    cursor: pointer;
    list-style: none;
  }
  .explainer-summary::-webkit-details-marker {
    display: none;
  }
  .explainer-summary:hover {
    background: var(--accent);
  }
  .explainer-summary:focus-visible {
    outline: none;
    background: var(--accent);
  }
  .explainer-eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
  }
  .explainer-hint {
    font-size: 11px;
    color: var(--ink-2);
    white-space: nowrap;
  }
  /* Coral +/x mark on the LEFT of the summary; rotates to an x on expand. */
  .explainer-mark {
    font-size: 18px;
    line-height: 1;
    color: var(--coral);
    transition: transform 150ms ease;
  }
  .explainer[open] .explainer-mark {
    transform: rotate(45deg);
  }
  .explainer-body {
    position: absolute;
    top: calc(100% + 8px);
    /* Anchored to the explainer's RIGHT edge: the explainer now lives on the
       right of the header row, so the popover must flow leftward to stay
       on-screen (a left-anchored popover would overflow the viewport edge). */
    right: 0;
    z-index: 50;
    width: max-content;
    max-width: min(62ch, calc(100vw - 48px));
    padding: 14px 16px;
    background: var(--paper);
    border: 2px solid var(--ink);
  }
  .explainer-body p {
    font-size: 12px;
    line-height: 1.6;
    color: var(--ink-2);
    margin: 0;
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

  /* ── view toggle (segmented control) ────────────────────────── */
  /* A single segmented box: the two tabs share one ink border with a divider
     between them. The active tab fills with the yellow accent. */
  .view-toggle {
    display: inline-flex;
    border: 2px solid var(--ink);
    background: var(--paper);
  }
  .view-toggle-btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
    padding: 8px 20px;
    border: none;
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
    transition: background 120ms ease;
  }
  .view-toggle-btn + .view-toggle-btn {
    border-left: 2px solid var(--ink);
  }
  .view-toggle-btn.on {
    background: var(--accent);
  }
  .view-toggle-btn:hover:not(.on) {
    background: var(--bg-elev);
  }

  /* ── chat box shell ─────────────────────────────────────────── */
  /* Fills the view-area (flex:1) so the chat surface is the full-screen app;
     the transcript scrolls internally and the input stays pinned. A 2.5px ink
     border reads as the heavier brutalist panel from the mockup. */
  .chat-box {
    flex: 1;
    min-height: 0;
    width: 100%;
    border: 2.5px solid var(--ink);
    background: var(--paper);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }
  /* The in-app header bar: PUBLIC CHAT tag + a pulsing-LED SESSION OPEN status,
     a flexible spacer, then NEW CHAT. */
  .panel-head {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 11px 16px;
    background: var(--paper);
    border-bottom: 2.5px solid var(--ink);
  }
  .panel-tag {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    padding: 4px 10px;
    border: 2px solid var(--ink);
    background: var(--ink);
    color: var(--paper);
  }
  .session {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--ink-3);
  }
  .session.on {
    color: var(--ink);
  }
  .led {
    width: 9px;
    height: 9px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    background: var(--coral);
    display: inline-block;
    flex-shrink: 0;
  }
  /* Green + pulsing only when a session is open. */
  .led.on {
    background: var(--green);
    animation: led-pulse 1.6s ease-in-out infinite;
  }
  @keyframes led-pulse {
    0%,
    100% {
      box-shadow: 0 0 0 0 var(--green);
      opacity: 1;
    }
    50% {
      box-shadow: 0 0 0 3px transparent;
      opacity: 0.55;
    }
  }
  .panel-spacer {
    flex: 1;
  }
  .bar-btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    padding: 5px 11px;
    border: 2px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
    transition:
      background 120ms ease,
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .bar-btn:hover {
    background: var(--accent);
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard-sm);
  }
  .bar-btn:disabled {
    cursor: default;
    opacity: 0.6;
  }
  .bar-btn:disabled:hover {
    background: var(--paper);
    transform: none;
    box-shadow: none;
  }
  /* Brief inline confirmation ("LINK COPIED") or the URL to copy by hand. */
  .share-feedback {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    color: var(--ink);
    background: var(--blue);
    border: 1.5px solid var(--ink);
    padding: 3px 8px;
    max-width: 40ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
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
    gap: 20px;
    background: var(--paper);
  }

  /* ── gate ───────────────────────────────────────────────────── */
  .chat-gate {
    margin: 8px 0 0;
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
  .chat-gate-copy {
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink-2);
    max-width: 54ch;
  }

  /* ── empty state (headline + sub + chips + doodles) ─────────── */
  /* The signature first-run view: a big lowercase mono headline with a yellow
     highlight on the last word, a flavour paragraph, the starter chips, and a
     few absolutely-positioned doodles scattered around the panel. */
  .chat-empty {
    position: relative;
    margin: 18px 0 0;
    display: flex;
    flex-direction: column;
    gap: 16px;
    align-items: flex-start;
  }
  .empty-headline {
    font-family: var(--mono);
    font-size: clamp(28px, 5vw, 46px);
    font-weight: 700;
    line-height: 1.05;
    letter-spacing: -0.02em;
    color: var(--ink);
    max-width: 16ch;
  }
  /* Yellow highlight behind the last word: a flat band that sits behind the
     glyphs (box-decoration-break keeps it intact if the word ever wraps). */
  .empty-hl {
    background: linear-gradient(
      transparent 8%,
      var(--accent) 8% 92%,
      transparent 92%
    );
    padding: 0 6px;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
  }
  .empty-sub {
    font-size: 13px;
    line-height: 1.65;
    color: var(--ink-2);
    max-width: 56ch;
  }
  .chat-examples {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 4px;
  }
  /* White paper chips with a hard 3px ink offset shadow; hover fills with the
     yellow accent and presses the shadow down. */
  .chat-example {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    padding: 10px 14px;
    border: 2px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
    box-shadow: 3px 3px 0 var(--ink);
    cursor: pointer;
    transition:
      background 120ms ease,
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .chat-example:hover {
    background: var(--accent);
    transform: translate(-1px, -1px);
    box-shadow: 4px 4px 0 var(--ink);
  }
  .chat-example:active {
    transform: translate(3px, 3px);
    box-shadow: 0 0 0 var(--ink);
  }

  /* ── doodles (decorative SVGs, hidden on small screens) ─────── */
  .doodle {
    position: absolute;
    color: var(--ink);
    pointer-events: none;
    z-index: 0;
  }
  .doodle-cloud {
    width: 64px;
    top: 4px;
    right: 10%;
  }
  .doodle-star {
    width: 34px;
    top: 96px;
    right: 4%;
  }
  .doodle-squiggle {
    width: 78px;
    top: 8px;
    right: 30%;
    color: var(--coral);
  }
  .doodle-diamond {
    width: 20px;
    bottom: 8px;
    right: 14%;
  }
  @media (max-width: 640px) {
    .doodle {
      display: none;
    }
  }

  /* ── turns ──────────────────────────────────────────────────── */
  .turn {
    min-width: 0;
  }
  /* User turn: a yellow bubble pinned to the right with a hard offset shadow. */
  .turn-user {
    align-self: flex-end;
    max-width: 80%;
    display: flex;
    justify-content: flex-end;
  }
  .user-bubble {
    background: var(--accent);
    border: 2px solid var(--ink);
    box-shadow: 3px 3px 0 var(--ink);
    padding: 11px 14px;
    font-size: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }
  /* Bot / graph turn: a blue uppercase model label, then the markdown body set
     off by a 3px ink left rule. */
  .turn-bot {
    align-self: flex-start;
    max-width: 100%;
    width: 100%;
  }
  .bot-label {
    display: inline-block;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--ink);
    background: var(--blue);
    border: 1.5px solid var(--ink);
    padding: 2px 8px;
    margin-bottom: 9px;
  }
  .turn-md {
    font-size: 13px;
    line-height: 1.65;
    min-width: 0;
    padding-left: 14px;
    border-left: 3px solid var(--ink);
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  /* Code / preformatted blocks from the model must wrap, not run off the page. */
  .turn-md :global(pre),
  .turn-md :global(code) {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-width: 100%;
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

  /* Blinking block cursor at the end of the streaming reply. */
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
    padding-left: 14px;
    border-left: 3px solid var(--rule-2);
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

  /* ── grounded chips (under bot turns) ───────────────────────── */
  .turn-touched {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
    margin-top: 12px;
    margin-left: 14px;
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
    padding: 3px 9px;
    border: 1.5px solid var(--ink);
    background: var(--blue);
    box-shadow: 2px 2px 0 var(--ink);
    cursor: pointer;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition:
      background 120ms ease,
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .touched-chip:hover {
    background: var(--accent);
    transform: translate(-1px, -1px);
    box-shadow: 3px 3px 0 var(--ink);
  }

  /* ── notice ─────────────────────────────────────────────────── */
  .chat-notice {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-top: 2.5px solid var(--ink);
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

  /* ── input dock ─────────────────────────────────────────────── */
  /* A bordered field with the text input, a char counter top-right, a blue SEND
     button with a hard offset shadow, and a small blue squiggle bottom-left. */
  .chat-input {
    position: relative;
    display: flex;
    gap: 10px;
    align-items: flex-end;
    padding: 12px 14px;
    border-top: 2.5px solid var(--ink);
    background: var(--paper);
  }
  .dock-doodle {
    position: absolute;
    left: 16px;
    bottom: 4px;
    width: 52px;
    pointer-events: none;
    opacity: 0.85;
  }
  @media (max-width: 640px) {
    .dock-doodle {
      display: none;
    }
  }
  .chat-input textarea {
    flex: 1;
    resize: none;
    min-height: 46px;
    max-height: 160px;
    font-family: var(--mono);
    font-size: 13px;
    line-height: 1.5;
    padding: 12px 13px;
    border: 2px solid var(--rule-2);
    border-radius: 0;
    background: var(--paper);
    color: var(--ink);
    transition: border-color 120ms ease;
  }
  .chat-input textarea:focus,
  .chat-input textarea:focus-visible {
    outline: none;
    border-color: var(--ink);
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
    min-height: 46px;
    padding: 0 22px;
    border: 2px solid var(--ink);
    background: var(--blue);
    color: var(--ink);
    box-shadow: 3px 3px 0 var(--ink);
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease,
      filter 120ms ease;
  }
  .chat-send:hover:not(:disabled) {
    transform: translate(-1px, -1px);
    box-shadow: 4px 4px 0 var(--ink);
  }
  .chat-send:active:not(:disabled) {
    transform: translate(3px, 3px);
    box-shadow: 0 0 0 var(--ink);
  }
  .chat-send:disabled {
    opacity: 0.5;
    box-shadow: none;
    cursor: default;
  }

  /* ── graph fallback (loading / error before the lazy view) ──── */
  .graph-fallback {
    flex: 1;
    min-height: 0;
    width: 100%;
    border: 2.5px solid var(--ink);
    background: var(--bg);
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
  }
  .graph-thinking {
    border-left: none;
    padding-left: 0;
  }

  /* ── reduced motion: no scroll, no LED pulse, no caret blink ── */
  @media (prefers-reduced-motion: reduce) {
    .ticker-track,
    .led.on,
    .caret,
    .turn-thinking .dot,
    .graph-thinking .dot {
      animation: none;
    }
  }

  @media (max-width: 640px) {
    .chat-app {
      gap: 10px;
      padding: calc(14px + env(safe-area-inset-top))
        calc(14px + env(safe-area-inset-right))
        calc(14px + env(safe-area-inset-bottom))
        calc(14px + env(safe-area-inset-left));
    }
    .explainer-hint {
      display: none;
    }
    .chat-transcript {
      padding: 16px;
      gap: 16px;
    }
    .turn-user {
      max-width: 92%;
    }
  }
</style>
