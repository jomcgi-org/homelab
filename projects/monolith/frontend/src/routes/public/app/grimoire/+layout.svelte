<script>
  // Public Grimoire app shell: a slim app-own topbar (wordmark + Library /
  // Entities nav) and a Turnstile gate around the app content. The gate is a
  // real admission boundary, not decoration: children (and therefore every
  // /api/grimoire fetch) only mount after onAdmitted fires, so the WotC-
  // copyrighted corpus never reaches an unsolved visitor or a crawler. No
  // site Nav/Footer: this is a standalone shareable app, not a page of the
  // portfolio site. noindex keeps it out of search entirely (copyright
  // mitigation) regardless of admission state.
  import { page } from "$app/stores";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";
  import { libraryHref, entitiesHref } from "$lib/public/grimoire/api.js";

  let { data, children } = $props();

  let admitted = $state(false);

  // Highlight the active topbar link: the entities index and entity detail
  // pages both count as "entities"; everything else is the library flow.
  const section = $derived.by(() => {
    const id = $page.route.id ?? "";
    if (id.includes("/entities") || id.includes("/entity/")) return "entities";
    return "library";
  });
</script>

<svelte:head>
  <title>Grimoire · jomcgi.dev</title>
  <meta
    name="description"
    content="A read-only, link-shareable D&D sourcebook library: browse loaded books, read chunk by chunk, and look up creatures and lore."
  />
  <!-- Link-shareable, not crawlable: the corpus is WotC-copyrighted, so this
       route is deliberately excluded from indexing and never added to
       sitemap.xml or a robots.txt allow. -->
  <meta name="robots" content="noindex, nofollow" />
</svelte:head>

<div class="grimoire-app">
  <header class="topbar">
    <a class="mono wordmark" href={libraryHref()}>GRIMOIRE</a>
    <nav class="topbar-nav" aria-label="Grimoire sections">
      <a
        class="mono topbar-link"
        class:active={section === "library"}
        href={libraryHref()}>LIBRARY</a
      >
      <a
        class="mono topbar-link"
        class:active={section === "entities"}
        href={entitiesHref()}>ENTITIES</a
      >
    </nav>
  </header>

  <main class="grimoire-shell">
  {#if !admitted}
    <div class="wrap-narrow gate">
      <p class="eyebrow">GRIMOIRE ACCESS</p>
      <h1 class="display gate-title">
        solve to <span class="hl-yellow">read.</span>
      </h1>
      <p class="gate-copy">
        One quick check keeps the bots out of a copyrighted sourcebook. The
        library loads right after.
      </p>
      <TurnstileGate
        siteKey={data.turnstileSiteKey}
        onAdmitted={() => (admitted = true)}
      />
    </div>
  {:else}
    {@render children()}
  {/if}
  </main>
</div>

<style>
  /* App-own chrome: a slim topbar plus hard-edge overrides. Setting the
     radius custom properties to 0 on the app root squares off every
     .card-hard / button / select inside via inheritance, without touching
     the rest of the public site. */
  .grimoire-app {
    --radius: 0px;
    --radius-lg: 0px;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
  }

  /* The design system's :focus-visible ring is rounded (6px); square it to
     match the hard-edge language inside the app. */
  .grimoire-app :global(:focus-visible) {
    border-radius: 0;
  }

  .topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 10px 32px;
    border-bottom: 2px solid var(--ink);
    background: var(--bg);
  }

  .wordmark {
    font-size: 14px;
    font-weight: 800;
    letter-spacing: 0.14em;
    color: var(--ink);
    text-decoration: none;
  }

  .topbar-nav {
    display: flex;
    gap: 6px;
  }

  .topbar-link {
    display: inline-flex;
    align-items: center;
    min-height: 36px;
    padding: 4px 12px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    text-decoration: none;
  }

  /* High-contrast state change, no lift: ink fill on hover/active. */
  .topbar-link:hover {
    background: var(--ink);
    color: var(--paper);
  }

  .topbar-link.active {
    background: var(--ink);
    color: var(--paper);
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

  .gate-title {
    font-size: clamp(32px, 6vw, 56px);
  }

  .gate-copy {
    max-width: 52ch;
    color: var(--ink-2);
    line-height: 1.6;
  }
</style>
