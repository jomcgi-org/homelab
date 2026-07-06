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
  import ThemeToggle from "$lib/grimoire/ThemeToggle.svelte";
  import "$lib/grimoire/theme.css";
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

<div class="grimoire-app grimoire">
  <header class="topbar">
    <a class="wordmark" href={libraryHref()}>Grimoire</a>
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
    </nav>
    <div class="topbar-spacer"></div>
    <ThemeToggle />
  </header>

  <main class="grimoire-shell">
  {#if !admitted}
    <div class="wrap-narrow gate">
      <p class="gate-eyebrow">Grimoire Access</p>
      <h1 class="grim-title gate-title">
        solve to <span class="gate-accent">read.</span>
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
  /* App-own chrome: a slim sticky topbar over the clean --grim-* theme. No
     more design-system .card-hard/.btn overrides here: every surface in this
     app tree now renders its own --grim-surface cards and hairline borders
     directly (see theme.css + each route's <style> block), so there is
     nothing left for a global override to neutralize. */
  .grimoire-app {
    min-height: 100vh;
    display: flex;
    flex-direction: column;
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
