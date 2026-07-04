<script>
  // Public Grimoire app shell: Nav, a Turnstile gate around the app content
  // (Library/sections/reader/entities), then Footer. The gate is a real
  // admission boundary, not decoration: children (and therefore every
  // /api/grimoire fetch) only mount after onAdmitted fires, so the WotC-
  // copyrighted corpus never reaches an unsolved visitor or a crawler. Nav and
  // Footer render unconditionally (bracketing the app, matching the public
  // site's chrome), noindex keeps this out of search entirely (Task 3 /
  // copyright mitigation) regardless of admission state.
  import Nav from "$lib/public/components/Nav.svelte";
  import Footer from "$lib/public/components/Footer.svelte";
  import TurnstileGate from "$lib/public/components/TurnstileGate.svelte";

  let { data, children } = $props();

  let admitted = $state(false);
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

<Nav route="grimoire" />

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

<Footer />

<style>
  /* Structural shell only: vertical breathing room between Nav and the app
     content / Footer. The gate copy reuses .wrap-narrow + .display/.eyebrow/
     .hl-yellow verbatim, so this is the only custom CSS the shell needs. */
  .grimoire-shell {
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
