<script>
  // Public Grimoire chat: a focused D&D Q&A box wired to the grimoire_chat
  // backend, which grounds every answer on the public Grimoire sourcebook
  // corpus (a Dungeon-Master / sage persona, no tools, no cloud). Ported from
  // the notes chat UI (routes/public/app/notes/+page.svelte): the Turnstile
  // admission gate, SSE streaming, transcript, NEW CHAT reset, and SHARE flow
  // are all preserved; the notes-only stats ticker, the CHAT/GRAPH toggle,
  // and the GraphView deep-dive are dropped (there is no graph view on this
  // tier, so a "grounded in" chip is a plain badge, not a navigation link).
  //
  // This page renders inside the grimoire layout's own Turnstile gate (see
  // +layout.svelte), which only admits the LIBRARY. Starting a chat session
  // is a SEPARATE admission (its own Turnstile solve, its own httpOnly "gcs"
  // cookie) because it opens a distinct budgeted backend session, exactly
  // mirroring how a shared-snapshot fork is a separate admission from a fresh
  // session on the notes surface.
  import { onMount } from "svelte";
  import { renderMarkdown } from "$lib/components/notes/markdown.js";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import {
    streamChatMessage,
    initialTurnState,
    applyFrame,
    CHARACTER_LIMIT,
  } from "$lib/public/grimoire/chat/stream.js";
  import { createChatSession } from "$lib/public/grimoire/chat/admission.js";
  import { freshChatState } from "$lib/public/grimoire/chat/chat-state.js";
  import { constellationStore } from "$lib/public/grimoire/constellation-store.js";
  import { highlightMentions } from "$lib/public/grimoire/chat/mention-highlight.js";
  import { exploreEgo, worldHref } from "$lib/public/grimoire/api.js";
  import SourceDrawer from "$lib/public/grimoire/chat/SourceDrawer.svelte";
  import MiniConstellation from "$lib/public/grimoire/MiniConstellation.svelte";

  let { data } = $props();

  // ── admission / session ──────────────────────────────────────────
  // Seeded from the server loader: a live session (a reload) hydrates as
  // already-admitted with its stored transcript, so the visitor resumes
  // rather than re-passing the gate. A cookieless visit hydrates as
  // { admitted: false, [] } and shows the gate.
  let admitted = $state(data.admitted ?? false);

  // ── transcript ───────────────────────────────────────────────────
  // Committed turns. Each: { role: "user" | "assistant", content, touched? }.
  let messages = $state(data.initialMessages ?? []);
  let input = $state("");
  let sending = $state(false);
  // The in-flight assistant turn (token stream + per-turn grounding set).
  let turn = $state(initialTurnState());
  // A soft "busy" / hard "error" notice for the last turn, with a retry handle.
  let notice = $state(null);
  let lastUserMessage = $state("");
  let inputEl = $state();
  let transcriptEl = $state();
  let controller = null;

  // ── grounding ────────────────────────────────────────────────────
  // Each committed message carries its own `touched` list (the Grimoire
  // sourcebook passages, chunks or entities, that turn grounded on), rendered
  // as "GROUNDED IN" badges directly under the reply. There is no graph view
  // on this tier to click through to, but every touched item now carries a
  // `kind` ("chunk" | "entity") plus the fields needed to fetch and deep-link
  // it (entity_type for an entity; book_id/chunk_ref for a chunk), so a chip
  // opens the source in a dismissable in-page drawer (SourceDrawer) instead
  // of a full navigation. There is still no need to accumulate a cross-turn
  // highlight set the way the notes chat's GraphView did: activeSource below
  // just tracks whichever single chip is currently open.
  const BOT_LABEL = "THE GRIMOIRE";
  // Same allow-list mention-highlight.js applies before interpolating
  // entity_type into a CSS custom-property name: entity_type is
  // corpus-controlled, and this chip's --chip-color also interpolates it.
  const TYPE_ALLOWLIST = /^[a-z_]+$/;

  // The touched item currently open in the SourceDrawer, or null when closed.
  let activeSource = $state(null);

  // ── session constellation ────────────────────────────────────────
  // A live graph of every entity touched ANYWHERE in the Grimoire app this
  // session (chat grounding here, plus World card opens and reader mention
  // taps from other pages), rendered as this page's wide-screen side panel
  // (MiniConstellation). It is the SAME store the cross-page
  // ConstellationDock reads (constellation-store.js); the chat page just
  // renders its own larger panel instead of the dock (see +layout.svelte's
  // showDock check) to avoid showing the same graph twice. Nodes come from
  // node_touched frames; edges only ever come from real exploreEgo(id)
  // responses intersected with the session's node set (constellation-state.js
  // guarantees no fabricated relationship). NEW CHAT clears the shared store;
  // a reloaded session seeds it from history on first mount only (see
  // onMount below) so remounting this page never re-seeds duplicate work.
  let constellation = $state({ nodes: [], ids: new Set(), edges: [] });
  const unsubscribeConstellation = constellationStore.subscribe((s) => {
    constellation = s;
  });

  // Fold one touched entity into the shared store and, if it is genuinely
  // new, fire a best-effort ego fetch for it.
  function ingestTouched(item) {
    const before = constellation.ids.has(item?.id);
    constellationStore.touch(item);
    if (before || !constellation.ids.has(item?.id)) return;
    exploreEgo(item.id)
      .then((ego) => {
        constellationStore.recordEgo(item.id, ego);
      })
      .catch(() => {});
  }

  // Starters that map to well-covered corpus material (a classic monster's
  // lair actions, a signature Curse of Strahd NPC, a core spell rule, and the
  // Death House introductory adventure), so they retrieve and ground well.
  const EXAMPLES = [
    "What are a beholder's lair actions?",
    "Tell me about Strahd von Zarovich",
    "How does counterspell work?",
    "What's in the Death House?",
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

  function renderReply(text, touched) {
    // Model output is untrusted (ADR 005 layer 8, applied identically to the
    // Grimoire sage persona). renderMarkdown HTML-escapes &<> on every path
    // and emits no raw HTML, links, or javascript:/data: URLs, so injected
    // markup renders as inert text and never reaches the DOM as live nodes
    // (covered by markdown.test.js XSS cases). The app sets no
    // Content-Security-Policy (deferred to a later hardening pass), so this
    // escaping is the protection that matters. An empty title map means
    // [[wikilinks]] (which the model never has reason to emit here) render as
    // inert text.
    //
    // highlightMentions runs on the FRESH renderMarkdown output only (never
    // on its own return value, per its header contract), underlining any
    // touched entity's name in its type color.
    return highlightMentions(renderMarkdown(text ?? "", new Map()), touched);
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
          if (frame.type === "node_touched" && frame.data?.kind === "entity") {
            ingestTouched(frame.data);
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
    turn = s.turn;
    notice = s.notice;
    input = s.input;
    lastUserMessage = s.lastUserMessage;
    // NEW CHAT opens a brand-new server session (see newChat's comment), so
    // any grounded entities belong to a conversation that no longer exists.
    constellationStore.clear();
  }

  // A reloaded session hydrates with its full transcript already committed
  // (see `messages` above), so fold every already-touched entity through the
  // same ingest path used for a live stream. The constellation panel then
  // renders fully revealed on first paint instead of replaying the reveal
  // animation for a conversation that already happened. Guarded on the store
  // already being empty: the store is session-persisted (sessionStorage), so
  // a remount of this same page (e.g. navigating away to World and back)
  // must not re-seed and duplicate ego fetches for entities the store
  // already has.
  onMount(() => {
    if (constellation.nodes.length > 0) return;
    for (const m of messages) {
      for (const item of m.touched ?? []) {
        if (item.kind === "entity") ingestTouched(item);
      }
    }
  });

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
  }

  // ── share this chat ──────────────────────────────────────────────
  // Opt-in, read-only share. POSTs to the same-origin /app/grimoire/chat/share
  // proxy (the session id rides the httpOnly cookie, never the body), gets
  // {snapshot_id}, and copies a share URL to the clipboard. The snapshot is
  // minted server-side from the stored transcript, so nothing here can forge
  // content.
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
      const resp = await fetch("/app/grimoire/chat/share", { method: "POST" });
      if (!resp.ok) {
        flashShare("COULD NOT SHARE");
        return;
      }
      const { snapshot_id: snapshotId } = await resp.json();
      const shareUrl = `${location.origin}/app/grimoire/chat/s/${snapshotId}`;
      try {
        await navigator.clipboard.writeText(shareUrl);
        flashShare("LINK COPIED");
      } catch {
        // Clipboard blocked (no permission / insecure context): surface the
        // URL so the visitor can copy it by hand.
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
    return () => {
      controller?.abort();
      unsubscribeConstellation();
    };
  });
</script>

<!-- Visually hidden heading: the topbar carries the app chrome, and the panel
     head below carries the in-app label, but keep a real h1 for a11y/SEO. -->
<h1 class="sr-only">Ask the Grimoire</h1>

<div class="chat-page">
  <section class="chat-box">
    <div class="panel-head">
      <span class="panel-tag">ASK THE GRIMOIRE</span>
      <span class="session" class:on={admitted}>
        <span class="led" class:on={admitted}></span>
        {admitted ? "SESSION OPEN" : "LOCKED"}
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

    <div class="chat-main">
      <div class="chat-transcript" bind:this={transcriptEl}>
        {#if !admitted}
          <div class="chat-gate">
            <p class="eyebrow chat-gate-eyebrow">START CHATTING</p>
            <p class="chat-gate-copy">
              Solve the puzzle once to start. Every answer shows the sourcebook
              page it came from.
            </p>
            <TurnstileGate
              siteKey={data.turnstileSiteKey}
              admit={createChatSession}
              onAdmitted={() => {
                admitted = true;
                queueMicrotask(() => inputEl?.focus());
              }}
            />
          </div>
        {:else if messages.length === 0 && !sending}
          <div class="chat-empty">
            <h2 class="grim-title empty-headline">
              ask the grimoire <span class="empty-hl">anything.</span>
            </h2>
            <p class="empty-sub">
              Rules, spells, monsters, magic items, lore, adventures. A sage
              reads the loaded sourcebooks and answers, citing what it drew on.
              Your questions never leave my homelab.
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
                <div class="turn-card">
                  <div class="turn-md">
                    {@html renderReply(m.content, m.touched)}
                  </div>
                  {#if m.touched && m.touched.length}
                    <div class="turn-touched">
                      <span class="turn-touched-label">GROUNDED IN</span>
                      {#each m.touched as n}
                        {#if n.kind === "entity"}
                          <a
                            href={worldHref(n.id)}
                            class="touched-chip touched-chip-entity"
                            style="--chip-color: var(--grim-type-{TYPE_ALLOWLIST.test(
                              n.entity_type ?? '',
                            )
                              ? n.entity_type
                              : 'class'}, currentColor)"
                          >
                            {n.title || "untitled"}
                          </a>
                        {:else}
                          <button
                            type="button"
                            class="touched-chip"
                            onclick={() => (activeSource = n)}
                          >
                            {n.title || "untitled passage"}
                          </button>
                        {/if}
                      {/each}
                    </div>
                  {/if}
                </div>
              </article>
            {/if}
          {/each}

          {#if sending}
            <article class="turn turn-bot">
              <p class="bot-label">{BOT_LABEL}</p>
              {#if turn.assistant}
                <div class="turn-card">
                  <div class="turn-md">
                    {@html renderReply(turn.assistant, turn.touched)}<span
                      class="caret"
                    ></span>
                  </div>
                </div>
              {:else}
                <p class="turn-thinking">
                  <span class="dot"></span><span class="dot"></span><span
                    class="dot"
                  ></span>
                  {turn.touched.length ? "reading the sourcebooks" : "thinking"}
                </p>
              {/if}
            </article>
          {/if}
        {/if}
      </div>

      {#if constellation.nodes.length > 0}
        <aside
          class="constellation"
          aria-label="People and places this conversation has drawn on"
        >
          <span class="constellation-cap">SESSION CONSTELLATION</span>
          <MiniConstellation
            nodes={constellation.nodes}
            edges={constellation.edges}
            revealedIds={new Set(constellation.ids)}
          />
        </aside>
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
          placeholder="Ask about a rule, a monster, a place..."
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
</div>

<SourceDrawer item={activeSource} onclose={() => (activeSource = null)} />

<style>
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

  /* ── page shell ──────────────────────────────────────────────
     Fills the viewport below the sticky 58px topbar (mirrors the explore
     canvas's calc(100dvh - 58px) convention), so the chat surface reads as a
     full-height app panel rather than a short card on a long page. */
  .chat-page {
    box-sizing: border-box;
    min-height: calc(100dvh - 58px);
    display: flex;
    padding: 20px 28px;
    font-family: var(--sans);
    color: var(--grim-ink);
  }

  .chat-box {
    flex: 1;
    min-height: 0;
    width: 100%;
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    background: var(--grim-surface);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  /* ── panel head bar ─────────────────────────────────────────── */
  .panel-head {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 18px;
    background: var(--grim-surface);
    border-bottom: 1px solid var(--grim-line);
  }
  .panel-tag {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    padding: 4px 10px;
    border-radius: 4px;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
  }
  .session {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--grim-text-faint);
  }
  .session.on {
    color: var(--grim-text-dim);
  }
  .led {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    border: 1.5px solid var(--grim-line);
    background: var(--grim-type-creature);
    display: inline-block;
    flex-shrink: 0;
  }
  .led.on {
    background: var(--grim-type-location);
    border-color: var(--grim-type-location);
    animation: led-pulse 1.6s ease-in-out infinite;
  }
  @keyframes led-pulse {
    0%,
    100% {
      box-shadow: 0 0 0 0 var(--grim-type-location);
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
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    padding: 6px 12px;
    border: 1px solid var(--grim-line);
    border-radius: 6px;
    background: var(--grim-surface);
    color: var(--grim-ink);
    cursor: pointer;
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }
  .bar-btn:hover {
    background: var(--grim-accent-soft);
    border-color: var(--grim-accent);
  }
  .bar-btn:disabled {
    cursor: default;
    opacity: 0.6;
  }
  .bar-btn:disabled:hover {
    background: var(--grim-surface);
    border-color: var(--grim-line);
  }
  .share-feedback {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--grim-ink);
    background: var(--grim-accent-soft);
    border: 1px solid var(--grim-accent);
    border-radius: 4px;
    padding: 3px 8px;
    max-width: 40ch;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* ── transcript + constellation row ────────────────────────────
     .chat-main sits between the panel head and the notice/input dock.
     flex:1/min-height:0 on both this row and .chat-transcript keeps the
     input dock pinned to the bottom of .chat-box while the transcript (and
     only the transcript) scrolls. */
  .chat-main {
    flex: 1;
    min-height: 0;
    display: flex;
  }
  .chat-transcript {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 28px;
    display: flex;
    flex-direction: column;
    gap: 22px;
    background: var(--grim-paper);
  }

  /* ── session constellation panel ───────────────────────────────
     A live graph of every entity the conversation has grounded on so far.
     Appears once (a single slide-in), then only individual node reveals
     animate (MiniConstellation's job, driven by revealedIds growing). */
  .constellation {
    flex: 0 0 280px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 16px 14px;
    border-left: 1px solid var(--grim-line);
    background: var(--grim-paper);
    overflow: hidden;
    animation: constel-in 0.45s cubic-bezier(0.22, 1, 0.36, 1);
  }
  @keyframes constel-in {
    from {
      width: 0;
      opacity: 0;
      padding-left: 0;
      padding-right: 0;
    }
    to {
      width: 280px;
      opacity: 1;
      padding-left: 14px;
      padding-right: 14px;
    }
  }
  .constellation-cap {
    font-family: var(--font-mono);
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: 0.13em;
    color: var(--grim-text-faint);
  }
  .constellation :global(.mini-stage) {
    flex: 1;
    min-height: 0;
  }
  /* GROUNDED IN chips remain the mobile grounding surface: the constellation
     panel needs width the small viewport does not have. */
  @media (max-width: 900px) {
    .constellation {
      display: none;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .constellation {
      animation: none;
    }
  }

  /* ── gate ───────────────────────────────────────────────────── */
  .chat-gate {
    margin: 8px 0 0;
    display: flex;
    flex-direction: column;
    gap: 12px;
    align-items: flex-start;
  }
  .eyebrow {
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }
  .chat-gate-copy {
    font-size: 13px;
    line-height: 1.6;
    color: var(--grim-text-dim);
    max-width: 54ch;
  }

  /* ── empty state ──────────────────────────────────────────────
     A quiet lowercase serif headline with an accent highlight on the last
     word, a flavour paragraph, and the starter chips. No brutalist doodles
     here: the grimoire's palette is a clean library, not a scrapbook. */
  .chat-empty {
    margin: 18px 0 0;
    display: flex;
    flex-direction: column;
    gap: 18px;
    align-items: flex-start;
  }
  .empty-headline {
    font-size: clamp(26px, 4.6vw, 42px);
    line-height: 1.12;
    max-width: 18ch;
  }
  .empty-hl {
    background: linear-gradient(
      transparent 8%,
      var(--grim-accent-soft) 8% 92%,
      transparent 92%
    );
    padding: 0 6px;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
  }
  .empty-sub {
    font-size: 13px;
    line-height: 1.65;
    color: var(--grim-text-dim);
    max-width: 58ch;
  }
  .chat-examples {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 2px;
  }
  .chat-example {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 600;
    padding: 10px 14px;
    border: 1px solid var(--grim-line);
    border-radius: 6px;
    background: var(--grim-surface);
    color: var(--grim-ink);
    cursor: pointer;
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }
  .chat-example:hover {
    background: var(--grim-accent-soft);
    border-color: var(--grim-accent);
  }

  /* ── turns ──────────────────────────────────────────────────── */
  .turn {
    min-width: 0;
  }
  .turn-user {
    align-self: flex-end;
    max-width: 80%;
    display: flex;
    justify-content: flex-end;
  }
  .user-bubble {
    background: var(--grim-accent-soft);
    border: 1px solid var(--grim-accent);
    border-radius: 12px;
    padding: 11px 14px;
    font-size: 13px;
    line-height: 1.55;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .turn-bot {
    align-self: flex-start;
    max-width: 100%;
    width: 100%;
  }
  .bot-label {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--grim-on-accent);
    background: var(--grim-accent);
    border-radius: 4px;
    padding: 2px 8px;
    margin: 0 0 9px;
  }
  /* The reply card: a near-white surface on the parchment background so the
     ANSWER text has real contrast, while the GROUNDED IN row (inside, as a
     muted footer) recedes instead of competing with it. */
  .turn-card {
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 12px;
    box-shadow: 0 1px 2px rgba(20, 24, 32, 0.05);
    padding: 16px 18px;
  }
  .turn-md {
    font-size: 13.5px;
    line-height: 1.68;
    min-width: 0;
    overflow-wrap: anywhere;
    word-break: break-word;
  }
  .turn-md :global(pre),
  .turn-md :global(code) {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-width: 100%;
    font-family: var(--font-mono);
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
    background: var(--grim-surface-2);
    padding: 1px 5px;
    border: 1px solid var(--grim-line);
    border-radius: 3px;
    font-size: 12px;
  }
  .turn-md :global(pre) {
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    border-radius: 6px;
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
    border-left: 2px solid var(--grim-accent);
    background: var(--grim-surface-2);
  }
  .turn-md :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 9px 0;
    font-size: 11.5px;
  }
  .turn-md :global(th),
  .turn-md :global(td) {
    border: 1px solid var(--grim-line);
    padding: 4px 8px;
    text-align: left;
  }
  .turn-md :global(th) {
    background: var(--grim-surface-2);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.06em;
    font-family: var(--font-mono);
  }
  /* Grounded entity mentions (highlightMentions): a visible, clickable link
     to the entity's World page. The color rides in via the inline
     `color` style the module sets per anchor; the pill background and
     underline both derive from it (currentColor) via color-mix so a new
     entity type never needs a new rule here. */
  .turn-md :global(.gmark) {
    font-weight: 600;
    text-decoration: underline;
    text-decoration-thickness: 1.5px;
    text-underline-offset: 2px;
    border-radius: 4px;
    padding: 0.5px 4px;
    margin: 0 -1px;
    background: color-mix(in srgb, currentColor 12%, transparent);
    transition: background 120ms ease;
  }
  .turn-md :global(.gmark:hover),
  .turn-md :global(.gmark:focus-visible) {
    background: color-mix(in srgb, currentColor 20%, transparent);
  }

  .caret {
    display: inline-block;
    width: 7px;
    height: 1.05em;
    margin-left: 2px;
    vertical-align: text-bottom;
    background: var(--grim-ink);
    animation: caret-blink 900ms steps(2, start) infinite;
  }
  @keyframes caret-blink {
    50% {
      opacity: 0;
    }
  }

  .turn-thinking {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--grim-text-faint);
    padding-left: 14px;
    border-left: 2px solid var(--grim-line);
  }
  .turn-thinking .dot {
    width: 5px;
    height: 5px;
    border-radius: 999px;
    background: var(--grim-text-faint);
    display: inline-block;
    animation: dot-pulse 1s ease-in-out infinite;
  }
  .turn-thinking .dot:nth-child(2) {
    animation-delay: 0.15s;
  }
  .turn-thinking .dot:nth-child(3) {
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

  /* ── grounded badges ─────────────────────────────────────────
     Quiet by design: this metadata row lives inside the white .turn-card as
     a muted footer, so it recedes while the answer text above it pops.
     Every touched item carries a kind ("chunk" | "entity") plus enough to
     fetch and deep-link it. Entity chips are real links to the World page
     (worldHref); their left dot picks up the entity's type color as a quiet
     visual echo of the inline mention links above. Non-entity (chunk) chips
     stay buttons that open the SourceDrawer, as before. */
  .turn-touched {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 7px;
    margin-top: 14px;
    padding-top: 10px;
    border-top: 1px dashed var(--grim-line);
  }
  .turn-touched-label {
    font-family: var(--font-mono);
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--grim-text-faint);
  }
  .touched-chip {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 3px 9px;
    border-radius: 999px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface-2);
    color: var(--grim-text-dim);
    text-decoration: none;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    cursor: pointer;
    transition:
      background 120ms ease,
      border-color 120ms ease,
      color 120ms ease;
  }
  .touched-chip:hover,
  .touched-chip:focus-visible {
    background: var(--grim-accent-soft);
    border-color: var(--grim-accent);
    color: var(--grim-ink);
  }
  /* Entity chips: a small dot tinted with the entity's type color, same
     currentColor-driven idea as .gmark above but a solid dot instead of a
     pill background, since this row wants to stay quiet. */
  .touched-chip-entity {
    color: var(--chip-color, var(--grim-text-dim));
  }
  .touched-chip-entity::before {
    content: "";
    width: 6px;
    height: 6px;
    border-radius: 999px;
    background: var(--chip-color, currentColor);
    flex-shrink: 0;
  }
  .touched-chip-entity:hover,
  .touched-chip-entity:focus-visible {
    color: var(--chip-color, var(--grim-ink));
    border-color: var(--chip-color, var(--grim-accent));
    background: color-mix(
      in srgb,
      var(--chip-color, var(--grim-accent)) 12%,
      transparent
    );
  }

  /* ── notice ─────────────────────────────────────────────────── */
  .chat-notice {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 18px;
    border-top: 1px solid var(--grim-line);
    background: color-mix(in srgb, var(--grim-type-creature) 12%, transparent);
    font-size: 12px;
  }
  .chat-notice.busy {
    background: var(--grim-accent-soft);
  }
  .chat-notice-text {
    flex: 1;
    color: var(--grim-text-dim);
  }
  .chat-notice-retry {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    padding: 5px 10px;
    border: 1px solid var(--grim-line);
    border-radius: 6px;
    background: var(--grim-surface);
    color: var(--grim-ink);
    cursor: pointer;
  }

  /* ── input dock ─────────────────────────────────────────────── */
  .chat-input {
    display: flex;
    gap: 10px;
    align-items: flex-end;
    padding: 14px 18px;
    border-top: 1px solid var(--grim-line);
    background: var(--grim-surface);
  }
  .chat-input textarea {
    flex: 1;
    resize: none;
    min-height: 46px;
    max-height: 160px;
    font-family: var(--sans);
    font-size: 13px;
    line-height: 1.5;
    padding: 12px 13px;
    border: 1px solid var(--grim-line);
    border-radius: 8px;
    background: var(--grim-paper);
    color: var(--grim-ink);
    transition: border-color 120ms ease;
  }
  .chat-input textarea:focus,
  .chat-input textarea:focus-visible {
    outline: none;
    border-color: var(--grim-accent);
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
    font-family: var(--font-mono);
    font-size: 9px;
    color: var(--grim-text-faint);
    letter-spacing: 0.04em;
  }
  .chat-count.warn {
    color: var(--grim-type-creature);
    font-weight: 700;
  }
  .chat-send {
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    min-height: 46px;
    padding: 0 22px;
    border: none;
    border-radius: 8px;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    cursor: pointer;
    transition:
      filter 120ms ease,
      opacity 120ms ease;
  }
  .chat-send:hover:not(:disabled) {
    filter: brightness(1.08);
  }
  .chat-send:disabled {
    opacity: 0.5;
    cursor: default;
  }

  /* ── reduced motion: no LED pulse, no caret blink ────────────── */
  @media (prefers-reduced-motion: reduce) {
    .led.on,
    .caret,
    .turn-thinking .dot {
      animation: none;
    }
  }

  @media (max-width: 640px) {
    .chat-page {
      padding: 12px;
      min-height: calc(100dvh - 58px);
    }
    .chat-transcript {
      padding: 18px;
      gap: 18px;
    }
    .turn-user {
      max-width: 92%;
    }
  }
</style>
