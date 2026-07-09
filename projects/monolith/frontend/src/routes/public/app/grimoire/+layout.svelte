<script>
  // Public Grimoire app shell: a slim app-own topbar (wordmark + Library /
  // Entities nav) and a Turnstile gate around the derived-corpus surfaces
  // (Entities, Explore, Chat, adventure detail): those only mount after
  // onAdmitted fires. The gate no longer wraps the Library or the reader:
  // the Library serves only book metadata plus open-licensed full text, and
  // the reader serves only open-licensed books (copyrighted books 403 and
  // show a locked notice), so neither carries gated corpus (see isUngated
  // below). The static homepage at the app root is ungated too. No site
  // Nav/Footer: this is a standalone shareable app, not a page of the
  // portfolio site. noindex keeps the whole tree out of search regardless of
  // admission state.
  import { page } from "$app/stores";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import "$lib/grimoire/theme.css";
  import {
    homeHref,
    libraryHref,
    entitiesHref,
    exploreHref,
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

  // Highlight the active topbar link: the entities index and entity detail
  // pages both count as "entities"; the EXPLORE canvas is its own section;
  // the homepage highlights nothing; every other page (library, book reader,
  // adventures) is the library flow.
  const section = $derived.by(() => {
    const id = $page.route.id ?? "";
    if (id.includes("/entities") || id.includes("/entity/")) return "entities";
    if (id.includes("/explore")) return "explore";
    if (id.includes("/chat")) return "chat";
    if (isHome) return "home";
    return "library";
  });
</script>

<svelte:head>
  <title>Grimoire · jomcgi.dev</title>
  <meta
    name="description"
    content="A read-only, link-shareable D&D sourcebook library: browse loaded books, read chunk by chunk, and look up creatures and lore."
  />
  <!-- Link-shareable, not crawlable: the whole app tree is kept out of search
       and off sitemap.xml / a robots.txt allow, regardless of admission. -->
  <meta name="robots" content="noindex, nofollow" />
</svelte:head>

<div class="grimoire-app grimoire" class:home={isHome}>
  <header class="topbar">
    <a class="wordmark" href={homeHref()}>Grimoire</a>
    <nav class="topbar-nav" aria-label="Grimoire sections">
      <a
        class="topbar-link"
        class:active={section === "library"}
        href={libraryHref()}>Library</a
      >
      <a
        class="topbar-link"
        class:active={section === "entities"}
        href={entitiesHref()}>Entities</a
      >
      <a
        class="topbar-link"
        class:active={section === "explore"}
        href={exploreHref()}>Explore</a
      >
      <a class="topbar-link" class:active={section === "chat"} href={chatHref()}
        >Chat</a
      >
    </nav>
    <div class="topbar-spacer"></div>
  </header>

  <main class="grimoire-shell">
  {#if isUngated || admitted}
    {@render children()}
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
    background: color-mix(in srgb, var(--grim-paper) 88%, transparent);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--grim-line);
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
    font-weight: 700;
    font-size: 14px;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    color: var(--grim-ink);
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
    color: var(--grim-text-faint);
    text-decoration: none;
    border-bottom: 2px solid transparent;
  }

  /* Quiet indigo underline on hover/active, no fill: matches the reskin's
     denoised chrome (the brutalist ink-block hover is gone). */
  .topbar-link:hover {
    color: var(--grim-text-dim);
  }

  .topbar-link.active {
    color: var(--grim-ink);
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
