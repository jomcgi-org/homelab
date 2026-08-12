<script>
  // Public Grimoire app shell: a slim app-own topbar (wordmark + Library /
  // World / Chat nav) and a Turnstile gate around the derived-corpus
  // surfaces (World, Chat, adventure detail): those only mount after
  // onAdmitted fires. The gate no longer wraps the Library or the reader:
  // the Library serves only book metadata plus open-licensed full text, and
  // the reader serves only open-licensed books (copyrighted books 403 and
  // show a locked notice), so neither carries gated corpus (see isUngated
  // below). The static homepage at the app root is ungated too. No site
  // Nav/Footer: this is a standalone shareable app, not a page of the
  // portfolio site. noindex keeps the whole tree out of search regardless of
  // admission state.
  //
  // World is the merged Entities+Explore surface (a later task); until that
  // route lands, the legacy /entities, /entity/*, and /explore pages keep
  // working directly and are treated as "world" for nav highlighting so the
  // active tab reads correctly no matter which URL a visitor lands on.
  import { page } from "$app/stores";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import PageTurn from "$lib/public/grimoire/PageTurn.svelte";
  import ConstellationDock from "$lib/public/grimoire/ConstellationDock.svelte";
  import "$lib/grimoire/theme.css";
  import {
    homeHref,
    libraryHref,
    worldHref,
    chatHref,
  } from "$lib/public/grimoire/api.js";

  let { data, children } = $props();

  let admitted = $state(false);

  // The homepage at the app root is a static pitch page: no corpus content,
  // no /api/grimoire fetches, so it renders OUTSIDE the Turnstile gate.
  const isHome = $derived(($page.route.id ?? "") === "/public/app/grimoire");

  // Routes that render outside the gate: the homepage, the Library (book list
  // + counts + open-licensed reader links), and the reader itself under
  // /book/* (serves only open-licensed text; copyrighted books 403). The gate
  // now protects only the derived-corpus surfaces (Entities, Explore, Chat,
  // adventure detail), which is where the whole corpus is surfaced.
  const isUngated = $derived.by(() => {
    const id = $page.route.id ?? "";
    return (
      isHome ||
      id === "/public/app/grimoire/library" ||
      id.startsWith("/public/app/grimoire/book/")
    );
  });

  // Highlight the active topbar link. World covers its own future route
  // (/world) plus the legacy /entities, /entity/*, and /explore paths it
  // will absorb: those still 404-redirect today (Task 5), but the nav
  // should already read as World when a visitor lands on one of them
  // directly (e.g. an old bookmarked link). Every other page (library,
  // book reader, adventures) is the library flow; the homepage highlights
  // nothing.
  const section = $derived.by(() => {
    const id = $page.route.id ?? "";
    if (
      id.includes("/world") ||
      id.includes("/entities") ||
      id.includes("/entity/") ||
      id.includes("/explore")
    )
      return "world";
    if (id.includes("/chat")) return "chat";
    if (isHome) return "home";
    return "library";
  });

  // Top-level section used to key the page-turn transition: the book/*
  // segment reads as its own section (distinct from the library index) even
  // though it's grouped with library for nav highlighting.
  const pageTurnSegment = $derived.by(() => {
    const id = $page.route.id ?? "";
    if (id.startsWith("/public/app/grimoire/book/")) return "book";
    return section;
  });

  // The chat page renders its own large "session constellation" panel
  // (routes/public/app/grimoire/chat/+page.svelte) fed by the same shared
  // store; showing the dock there too would double the same graph on
  // screen, so the dock skips the chat route specifically.
  const showDock = $derived(!($page.route.id ?? "").includes("/chat"));
</script>

<!-- The landing page (isHome) sets its own title, description, and robots
     meta in its +page.svelte: Svelte does not let a page override a layout's
     svelte:head tags, it only appends, so duplicating a conflicting title or
     robots tag here would leave two of each in the rendered head with no
     well-defined winner. Every other route in this tree stays link-shareable
     but not crawlable (kept out of search and off any sitemap/robots allow,
     regardless of admission), which is what this block covers. svelte:head
     itself cannot sit inside an {#if}, so the condition is on its children. -->
<svelte:head>
  {#if !isHome}
    <title>Grimoire · jomcgi.dev</title>
    <meta
      name="description"
      content="Browse the D&D sourcebooks loaded here, read them page by page, and look up creatures, places and lore."
    />
    <meta name="robots" content="noindex, nofollow" />
  {/if}
</svelte:head>

<div class="grimoire-app grimoire" class:home={isHome}>
  <header class="topbar">
    <a class="wordmark grim-title" href={homeHref()}>Grimoire</a>
    <nav class="topbar-nav" aria-label="Grimoire sections">
      <a
        class="topbar-link"
        class:active={section === "library"}
        href={libraryHref()}>Library</a
      >
      <a
        class="topbar-link"
        class:active={section === "world"}
        href={worldHref()}>World</a
      >
      <a class="topbar-link" class:active={section === "chat"} href={chatHref()}
        >Chat</a
      >
    </nav>
    <div class="topbar-spacer"></div>
  </header>

  <main class="grimoire-shell">
    {#if isUngated || admitted}
      <PageTurn section={pageTurnSegment}>
        {@render children()}
      </PageTurn>
    {:else}
      <div class="wrap-narrow gate">
        <p class="gate-eyebrow">Grimoire Access</p>
        <h1 class="grim-title gate-title">
          solve to <span class="gate-accent">explore.</span>
        </h1>
        <p class="gate-copy">
          Bots? <em>Get outta here!</em>
        </p>
        <TurnstileGate
          siteKey={data.turnstileSiteKey}
          onAdmitted={() => (admitted = true)}
        />
      </div>
    {/if}
  </main>

  {#if showDock}
    <ConstellationDock />
  {/if}
</div>

<style>
  /* App-own chrome: a slim sticky topbar over the clean --grim-* theme. No
     more design-system .card-hard/.btn overrides here: every surface in this
     app tree now renders its own --grim-surface cards and hairline borders
     directly (see theme.css + each route's <style> block), so there is
     nothing left for a global override to neutralize. */
  .grimoire-app {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    /* Own the page background so every grimoire page is on the clean --grim
       paper (not the site-wide design-system cream body showing behind the
       cards). */
    background: var(--grim-paper);
  }

  .topbar {
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    align-items: center;
    gap: 20px;
    padding: 0 28px;
    height: 58px;
    /* Dark ledger-cover surface, not the light reading paper: the topbar is
       the book's spine, so it should read as its own material rather than a
       plain white app header. */
    background: color-mix(in srgb, var(--grim-nav-surface) 92%, transparent);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--grim-nav-line);
    transition: opacity 0.25s ease;
  }

  /* The landing's ScrollStory toggles .story-immersed on .grimoire-app while
     the visitor is inside the story (past the hero, before the finale): the
     chrome gets out of the way of the pinned stage and returns at the end. */
  .grimoire-app:global(.story-immersed) .topbar {
    opacity: 0;
    pointer-events: none;
  }

  /* On phones the landing is the scroll story itself, which is its own hero and
     ends with call-to-action buttons, so the app chrome is pure clutter (and
     the four nav links overflow the width). Drop the whole topbar on the home
     route below the mobile breakpoint; every other route keeps it. */
  @media (max-width: 700px) {
    .grimoire-app.home .topbar {
      display: none;
    }
  }

  .wordmark {
    /* .grim-title already sets the serif display face + weight; this layers
       size, tracking, and the ledger-cover ink color on top of it. */
    font-size: 17px;
    letter-spacing: 0.06em;
    color: var(--grim-nav-ink);
    text-decoration: none;
    flex: none;
  }

  .topbar-nav {
    display: flex;
    gap: 4px;
    margin-left: 8px;
  }

  .topbar-link {
    display: inline-flex;
    align-items: center;
    min-height: 40px;
    padding: 6px 12px;
    margin-bottom: -1px;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--grim-nav-text-dim);
    text-decoration: none;
    border-bottom: 2px solid transparent;
  }

  /* Weighted accent underline on hover/active against the dark ledger
     surface, no fill: heavier than the reskin's light-mode hairline so it
     still reads clearly on the darker chrome. */
  .topbar-link:hover {
    color: var(--grim-nav-ink);
  }

  .topbar-link.active {
    color: var(--grim-nav-ink);
    border-bottom-width: 3px;
    border-bottom-color: var(--grim-accent);
  }

  .topbar-spacer {
    flex: 1;
  }

  .grimoire-shell {
    flex: 1;
    min-height: 60vh;
  }

  .gate {
    padding: 64px 32px 80px;
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 16px;
  }

  .gate-eyebrow {
    margin: 0;
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    font-weight: 600;
    color: var(--grim-text-faint);
  }

  .gate-title {
    font-size: clamp(32px, 6vw, 56px);
  }

  .gate-accent {
    color: var(--grim-accent);
  }

  .gate-copy {
    max-width: 52ch;
    color: var(--grim-text-dim);
    line-height: 1.6;
  }
</style>
