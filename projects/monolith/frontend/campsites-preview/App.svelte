<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import PublicLayout from "../src/routes/public/+layout.svelte";
  import Campsites from "../src/routes/public/app/campsites/+page.svelte";
  import Engineering from "../src/routes/public/engineering/+page.svelte";
  import Ember from "../src/routes/public/ember/+page.svelte";
  // Root error preloads the retained legacy stylesheet in production.
  import RootError from "../src/routes/+error.svelte";
  import { setPage } from "../test/profile-page.svelte.js";
  import { reroute } from "../src/hooks.js";
  import { snapshot } from "./fixtures.js";
  let route = $state("");
  let data = $state({});
  function navigate(url) {
    route = reroute({ url }) ?? url.pathname;
    data = {
      snapshot: url.searchParams.has("empty") ? null : snapshot,
      hasBlog: true,
    };
    setPage(url.href, data);
  }
  navigate(new URL(location.href));
  function click(event) {
    const anchor = event.target.closest("a");
    if (
      !anchor ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      anchor.target ||
      anchor.hasAttribute("download")
    )
      return;
    const next = new URL(anchor.href);
    if (
      next.origin !== location.origin ||
      next.hash ||
      !["/app/campsites", "/engineering", "/ember", "/legacy-fixture"].includes(
        next.pathname,
      )
    )
      return;
    event.preventDefault();
    history.pushState({}, "", next);
    navigate(next);
    window.scrollTo(0, 0);
  }
</script>

<svelte:window
  onclick={click}
  onpopstate={() => navigate(new URL(location.href))}
/>
<RootLayout>
  <PublicLayout {data}>
    {#if route === "/public/app/campsites"}<Campsites {data} />
    {:else if route === "/public/engineering"}<Engineering />
    {:else if route === "/public/ember"}<Ember
        data={{ status: null, savings: null }}
      />
    {:else if route === "/public/legacy-fixture"}<main>
        <a class="btn" href="/app/campsites">Legacy control</a>
      </main>
    {:else}<RootError />{/if}
  </PublicLayout>
</RootLayout>
<nav class="fixture-links" aria-label="Fixture routes">
  Synthetic parks, repository preview.
  <a href="/app/campsites">Campsites fixture</a>
  <a href="/engineering">Engineering fixture</a>
  <a href="/ember">Legacy Ember fixture</a>
  <a href="/legacy-fixture">Legacy Nav fixture</a>
</nav>

<style>
  .fixture-links {
    padding: 1rem;
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
    font: 12px monospace;
    background: var(--ds-surface);
    color: var(--ds-ink);
  }
</style>
