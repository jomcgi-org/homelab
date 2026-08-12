<script>
  // Read-only view of a shared Grimoire chat snapshot (ADR 005 "share this
  // chat", ported to the grimoire_chat surface). The transcript was minted
  // server-side from the stored record and is immutable. There is NO input
  // box, NO Turnstile, and NO session here: this page only renders a frozen
  // transcript. The model output is untrusted (ADR 005 layer 8), so it is
  // escaped through the same renderMarkdown the live grimoire chat uses
  // (HTML-escapes &<> on every path, emits no raw HTML, no links, no
  // javascript:/data: URLs), and rendered with the same user-bubble / bot-turn
  // look as the live app.
  //
  // Layout note: this route lives under routes/public/app/grimoire/, whose
  // +layout.svelte wraps every child in its own Turnstile gate ("solve to
  // read") before rendering anything -- fine for the library/chat surfaces,
  // but wrong here: a shared snapshot must render for an unadmitted visitor
  // with no challenge at all (only the "continue this chat" fork below is
  // gated). So this file is named +page@.svelte, SvelteKit's layout-reset
  // syntax, which skips every intermediate +layout.svelte (including the
  // grimoire gate) and inherits directly from the root layout. That also
  // means the .grimoire token scope (normally established by the grimoire
  // +layout.svelte) isn't ambient here, so this page establishes it itself:
  // import theme.css and put the .grimoire class on its own root element.
  // var(--font-mono) is used instead of the design-system.css var(--mono):
  // it resolves from the globally-loaded tokens.css (a system stack, no
  // webfont), so it renders correctly even without the public-tier layout
  // that this page has reset past.
  import { goto } from "$app/navigation";
  import { renderMarkdown } from "$lib/components/notes/markdown.js";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import { forkChatSession } from "$lib/public/grimoire/chat/admission.js";
  import { highlightMentions } from "$lib/public/grimoire/chat/mention-highlight.js";
  import { worldHref } from "$lib/public/grimoire/api.js";
  import "$lib/grimoire/theme.css";

  let { data } = $props();

  const BOT_LABEL = "THE GRIMOIRE";
  // Same allow-list mention-highlight.js applies before interpolating
  // entity_type into a CSS custom-property name: entity_type is
  // corpus-controlled, and this chip's --chip-color also interpolates it.
  const TYPE_ALLOWLIST = /^[a-z_]+$/;

  function renderReply(text, touched) {
    // Empty title map: [[wikilinks]] render as inert text (no graph nav here).
    // highlightMentions runs on the FRESH renderMarkdown output only (never
    // on its own return value, per its header contract).
    return highlightMentions(renderMarkdown(text ?? "", new Map()), touched);
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
    // The fork set the httpOnly "gcs" session cookie; the live app's loader
    // reads it and rehydrates the seeded transcript. Navigate there to
    // continue.
    goto("/app/grimoire/chat");
  }
</script>

<svelte:head>
  <title>Shared grimoire chat · jomcgi.dev</title>
  <!-- Human-shareable, not search-indexed: a snapshot is an anonymous
       visitor's link, not corpus content we want crawled. -->
  <meta name="robots" content="noindex" />
  <meta
    name="description"
    content="A read-only snapshot of a conversation with the Grimoire, a D&D sourcebook sage grounded in my loaded sourcebooks."
  />
</svelte:head>

<h1 class="sr-only">A shared conversation with the Grimoire</h1>

<main class="share-app grimoire">
  <header class="app-header">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="https://jomcgi.dev/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
        ></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">shared grimoire chat</span>
    </nav>
  </header>

  <section class="chat-box">
    <div class="panel-head">
      <span class="panel-tag">SHARED GRIMOIRE CHAT</span>
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
      <a class="bar-btn" href="/app/grimoire/chat">ASK THE GRIMOIRE</a>
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
              <div class="turn-card">
                <div class="turn-md">
                  {@html renderReply(m.content, m.touched)}
                </div>
                {#if m.touched && m.touched.length}
                  <!-- The same GROUNDED IN set the live app shows, persisted on
                       the assistant turn and carried into the snapshot.
                       Entity touches link to the World page, same as the
                       live app; chunk touches stay plain (no drawer here in
                       a read-only snapshot). -->
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
                          {n.title || "untitled entity"}
                        </a>
                      {:else}
                        <span class="touched-chip"
                          >{n.title || "untitled passage"}</span
                        >
                      {/if}
                    {/each}
                  </div>
                {/if}
              </div>
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
          The transcript above is carried over. No sign-in, no tracking beyond
          what keeps the bots out.
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
        A saved copy of a conversation with the Grimoire, a sage that answers
        from my D&D sourcebooks.
      </p>
      <a class="share-cta" href="/app/grimoire/chat">Ask the Grimoire &rarr;</a>
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
    box-sizing: border-box;
    max-width: 860px;
    margin: 0 auto;
    padding: 24px 18px 48px;
    font-family: var(--font-mono);
    color: var(--grim-ink);
    background: var(--grim-paper);
    min-height: 100vh;
  }
  .share-app *,
  .share-app *::before,
  .share-app *::after {
    box-sizing: border-box;
  }

  .app-header {
    margin-bottom: 16px;
  }
  .crumb {
    font-family: var(--font-mono);
    font-size: 12px;
    letter-spacing: 0.04em;
  }
  .crumb-home {
    color: var(--grim-ink);
    text-decoration: none;
    border-bottom: 2px solid var(--grim-accent);
  }
  .crumb-arrow {
    margin-left: 1px;
  }
  .crumb-sep {
    margin: 0 6px;
    color: var(--grim-text-dim);
  }
  .crumb-name {
    color: var(--grim-text-dim);
  }

  .chat-box {
    border: 1px solid var(--grim-line);
    border-radius: 10px;
    background: var(--grim-surface);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .panel-head {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 18px;
    border-bottom: 1px solid var(--grim-line);
    background: var(--grim-surface);
  }
  .panel-tag {
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    border-radius: 4px;
    padding: 4px 10px;
  }
  .panel-readonly {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--grim-text-dim);
    background: var(--grim-surface-2);
    border: 1px solid var(--grim-line);
    border-radius: 4px;
    padding: 2px 8px;
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
    text-decoration: none;
    cursor: pointer;
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }
  .bar-btn:hover {
    background: var(--grim-accent-soft);
    border-color: var(--grim-accent);
  }
  /* The primary "continue this chat" action: filled with the grimoire accent
     so it reads as the main affordance next to the neutral "ask the
     grimoire" link. */
  .bar-btn-primary {
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    border-color: var(--grim-accent);
  }
  .bar-btn-primary:hover {
    background: var(--grim-accent-strong);
    border-color: var(--grim-accent-strong);
  }

  /* The fork gate panel: revealed under the transcript when a visitor opts to
     continue. */
  .fork-panel {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 18px 24px;
    border-top: 1px solid var(--grim-line);
    background: var(--grim-surface);
  }
  .fork-eyebrow {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.16em;
    margin: 0;
    color: var(--grim-ink);
  }
  .fork-copy {
    font-size: 13px;
    line-height: 1.6;
    color: var(--grim-text-dim);
    max-width: 60ch;
    margin: 0;
  }

  .chat-transcript {
    padding: 24px;
    display: flex;
    flex-direction: column;
    gap: 20px;
    background: var(--grim-paper);
  }
  .share-empty {
    font-size: 13px;
    color: var(--grim-text-dim);
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
    background: var(--grim-accent-soft);
    border: 1px solid var(--grim-accent);
    border-radius: 10px;
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
    margin-bottom: 9px;
  }
  /* The reply card: a near-white surface on the parchment background so the
     ANSWER text has real contrast, matching the live chat app. */
  .turn-card {
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 12px;
    box-shadow: 0 1px 2px rgba(20, 24, 32, 0.05);
    padding: 16px 18px;
  }
  .turn-md {
    font-size: 13px;
    line-height: 1.65;
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
     to the entity's World page, matching the live chat app. */
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

  /* Grounding chips under a bot turn: quiet footer inside the .turn-card,
     same look as the live app's GROUNDED IN row. Entity touches link to the
     World page; chunk touches stay static (no drawer in a read-only
     snapshot). */
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
    transition:
      background 120ms ease,
      border-color 120ms ease,
      color 120ms ease;
  }
  .touched-chip-entity {
    color: var(--chip-color, var(--grim-text-dim));
    cursor: pointer;
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

  .share-foot {
    padding: 16px 24px 20px;
    border-top: 1px solid var(--grim-line);
    background: var(--grim-surface);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }
  .share-foot-copy {
    font-size: 12px;
    line-height: 1.55;
    color: var(--grim-text-dim);
    max-width: 60ch;
    margin: 0;
  }
  .share-cta {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--grim-on-accent);
    text-decoration: none;
    background: var(--grim-accent);
    border-radius: 6px;
    padding: 7px 12px;
    white-space: nowrap;
  }
  .share-cta:hover {
    background: var(--grim-accent-strong);
  }
</style>
