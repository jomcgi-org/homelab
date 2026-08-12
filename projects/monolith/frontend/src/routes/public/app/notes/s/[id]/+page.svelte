<script>
  // Read-only view of a shared chat snapshot (ADR 005 "share this chat"). The
  // transcript was minted server-side from the stored record and is immutable.
  // There is NO input box, NO Turnstile, and NO session here: this page only
  // renders a frozen transcript. The model output is untrusted (ADR 005 layer
  // 8), so it is escaped through the same renderMarkdown the live notes app uses
  // (HTML-escapes &<> on every path, emits no raw HTML, no links, no
  // javascript:/data: URLs), and rendered with the same user-bubble / bot-turn
  // look as the live app.
  import { goto } from "$app/navigation";
  import { renderMarkdown } from "$lib/components/notes/markdown.js";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import { forkChatSession } from "$lib/public/chat/admission.js";

  let { data } = $props();

  const BOT_LABEL = "QWEN3.6 / LOCAL";

  function renderReply(text) {
    // Empty title map: [[wikilinks]] render as inert text (no graph nav here).
    return renderMarkdown(text ?? "", new Map());
  }

  // ── fork this chat ───────────────────────────────────────────────
  // A snapshot is read-only, but a visitor can FORK it: solving a Turnstile
  // challenge mints a new live session seeded with this snapshot's transcript
  // (server-side), then we land them on the live app to keep chatting. The
  // challenge is the same admission gate as starting a fresh chat (a fork is a
  // new inference-backed session). Only offered when a site key is configured
  // and there is a transcript to continue.
  const canFork = $derived(
    Boolean(data.turnstileSiteKey) && data.messages.length > 0,
  );
  let forking = $state(false); // gate revealed?

  function startFork() {
    forking = true;
  }

  function onForked() {
    // The fork set the httpOnly session cookie; the live app's loader reads it
    // and rehydrates the seeded transcript. Navigate there to continue.
    goto("/app/notes");
  }
</script>

<svelte:head>
  <title>Shared chat · jomcgi.dev</title>
  <!-- Human-shareable, not search-indexed: a snapshot is an anonymous user's
       link, not site content we want crawled. -->
  <meta name="robots" content="noindex" />
  <meta
    name="description"
    content="A read-only snapshot of a chat with my public knowledge graph."
  />
</svelte:head>

<h1 class="sr-only">A shared chat with my knowledge graph</h1>

<main class="share-app">
  <header class="app-header">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
        ></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">shared chat</span>
    </nav>
  </header>

  <section class="chat-box">
    <div class="panel-head">
      <span class="panel-tag">SHARED CHAT</span>
      <span class="panel-readonly">READ-ONLY</span>
      <span class="panel-spacer"></span>
      {#if canFork && !forking}
        <button
          type="button"
          class="bar-btn bar-btn-primary"
          onclick={startFork}
        >
          CONTINUE THIS CHAT
        </button>
      {/if}
      <a class="bar-btn" href="/app/notes">START YOUR OWN CHAT</a>
    </div>

    <div class="chat-transcript">
      {#if data.messages.length === 0}
        <p class="share-empty">This shared chat is empty.</p>
      {:else}
        {#each data.messages as m}
          {#if m.role === "user"}
            <article class="turn turn-user">
              <div class="user-bubble">{m.content}</div>
            </article>
          {:else}
            <article class="turn turn-bot">
              <p class="bot-label">{BOT_LABEL}</p>
              <div class="turn-md">{@html renderReply(m.content)}</div>
              {#if m.touched && m.touched.length}
                <!-- The same BASED ON set the live app shows, persisted on
                     the assistant turn and carried into the snapshot. Static
                     labels here (read-only view: there is no graph to open). -->
                <div class="turn-touched">
                  <span class="turn-touched-label">BASED ON</span>
                  {#each m.touched as n}
                    <span class="touched-chip"
                      >{n.title || "untitled note"}</span
                    >
                  {/each}
                </div>
              {/if}
            </article>
          {/if}
        {/each}
      {/if}
    </div>

    {#if forking}
      <div class="fork-panel">
        <p class="fork-eyebrow">CONTINUE THIS CHAT</p>
        <p class="fork-copy">
          Solve the challenge to pick up this conversation in a fresh session.
          No sign-in, no tracking beyond what keeps the bots out.
        </p>
        <TurnstileGate
          siteKey={data.turnstileSiteKey}
          admit={(token) => forkChatSession(data.snapshotId, token)}
          onAdmitted={onForked}
        />
      </div>
    {/if}

    <div class="share-foot">
      <p class="share-foot-copy">
        A frozen copy of a chat with a model running on my own machines.
      </p>
      <a class="share-cta" href="/app/notes">Start your own chat &rarr;</a>
    </div>
  </section>
</main>

<style>
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .share-app {
    max-width: 860px;
    margin: 0 auto;
    padding: 24px 18px 48px;
    font-family: var(--mono);
    color: var(--ink);
  }

  .app-header {
    margin-bottom: 16px;
  }
  .crumb {
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.04em;
  }
  .crumb-home {
    color: var(--ink);
    text-decoration: none;
    border-bottom: 2px solid var(--blue);
  }
  .crumb-arrow {
    margin-left: 1px;
  }
  .crumb-sep {
    margin: 0 6px;
    color: var(--ink-2, #6b6b6b);
  }
  .crumb-name {
    color: var(--ink-2, #6b6b6b);
  }

  .chat-box {
    border: 2.5px solid var(--ink);
    background: var(--paper);
    box-shadow: var(--shadow-hard);
    display: flex;
    flex-direction: column;
  }

  .panel-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 14px;
    border-bottom: 2.5px solid var(--ink);
    background: var(--paper);
  }
  .panel-tag {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.16em;
    background: var(--accent);
    border: 2px solid var(--ink);
    padding: 3px 9px;
  }
  .panel-readonly {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    color: var(--ink);
    background: var(--blue);
    border: 1.5px solid var(--ink);
    padding: 2px 8px;
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
    text-decoration: none;
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
  /* The primary "continue this chat" action: filled blue so it reads as the
     main affordance next to the neutral "start your own chat" link. */
  .bar-btn-primary {
    background: var(--blue);
  }

  /* The fork gate panel: revealed under the transcript when a visitor opts to
     continue. The same neo-brutalist framing as the rest of the share view. */
  .fork-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 24px;
    border-top: 2.5px solid var(--ink);
    background: var(--paper);
  }
  .fork-eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.16em;
    margin: 0;
  }
  .fork-copy {
    font-size: 13px;
    line-height: 1.6;
    color: var(--ink-2, #6b6b6b);
    max-width: 60ch;
    margin: 0;
  }

  .chat-transcript {
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    background: var(--paper);
  }
  .share-empty {
    font-size: 13px;
    color: var(--ink-2, #6b6b6b);
  }

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
    background: var(--accent);
    border: 2px solid var(--ink);
    box-shadow: 3px 3px 0 var(--ink);
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
    background: var(--bg-elev, #f4f4f4);
    padding: 1px 5px;
    border: 1px solid var(--rule-2, #d9d9d9);
    font-size: 12px;
  }
  .turn-md :global(pre) {
    background: var(--bg-elev, #f4f4f4);
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
    background: var(--bg-elev, #f4f4f4);
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
    background: var(--bg-elev, #f4f4f4);
    text-transform: uppercase;
    font-size: 10px;
    letter-spacing: 0.06em;
  }

  /* Grounding chips under a bot turn: same look as the live app's BASED ON
     row, but static (no graph to open in a read-only snapshot). */
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
    color: var(--ink-3, #9b9b9b);
  }
  .touched-chip {
    font-family: var(--mono);
    font-size: 11px;
    padding: 3px 9px;
    border: 1.5px solid var(--ink);
    background: var(--blue);
    box-shadow: 2px 2px 0 var(--ink);
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .share-foot {
    padding: 16px 24px 20px;
    border-top: 2.5px solid var(--ink);
    background: var(--paper);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .share-foot-copy {
    font-size: 12px;
    line-height: 1.55;
    color: var(--ink-2, #6b6b6b);
    max-width: 60ch;
    margin: 0;
  }
  .share-cta {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--ink);
    text-decoration: none;
    background: var(--accent);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard-sm);
    padding: 7px 12px;
    white-space: nowrap;
  }
  .share-cta:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard);
  }
</style>
