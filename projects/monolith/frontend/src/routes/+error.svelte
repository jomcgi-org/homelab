<script>
  // Root error boundary. A "route not found" 404 never matches the /public or
  // /private route groups, so it surfaces here at the root, wrapped only by
  // src/routes/+layout.svelte. That root layout loads global.css (the legacy
  // mono token set) but not the technical-drawing sheet or its webfont (those
  // arrive with the public layout, which a 404 never reaches), so this page
  // pulls both in itself.
  import "$lib/public/styles/technical-drawing.css";
  import { TechnicalDrawingChrome, Trail } from "$lib/public/components";
  import { page } from "$app/stores";

  let status = $derived($page.status);
  let isNotFound = $derived(status === 404);

  // The browser path the visitor actually typed. $page.url reflects the
  // browser URL, not the hooks.js reroute target, so this shows "/asda"
  // rather than the internal "/public/asda".
  let path = $derived($page.url.pathname);

  let band = $derived(
    isNotFound
      ? "Error 404 / off the map"
      : `Error ${status} / something broke`,
  );
  let headline = $derived(
    isNotFound ? "you wandered off the map." : "the server hit a rough patch.",
  );
  let blurb = $derived(
    isNotFound
      ? "this page never existed, or it got refactored into the void. either way, there's nothing docked here."
      : ($page.error?.message ??
          "an unexpected error knocked this page over. it's not you, it's the cluster."),
  );
</script>

<svelte:head>
  <title>{status} · jomcgi.dev</title>
  <meta name="robots" content="noindex" />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin="anonymous"
  />
  <link
    href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<TechnicalDrawingChrome />

<main class="td sheet error-page">
  <div class="frame">
    <div class="journal single">
      <div class="content">
        <div class="trail-inline">
          <Trail crumbs={[{ label: String(status) }]} />
        </div>

        <article class="edition" aria-labelledby="error-h">
          <div class="ed-head">
            <span>{band}</span>
          </div>
          <div class="ed-lead">
            <h1 id="error-h">{headline}</h1>
            <p>{blurb}</p>
          </div>
          {#if isNotFound}
            <dl class="spec">
              <div class="row">
                <dt>You tried</dt>
                <dd class="path">{path}</dd>
              </div>
            </dl>
          {/if}
          <div class="ed-foot">
            <!-- Relative, NOT https://jomcgi.dev/. This page renders on every host
                 that serves the bundle (private.jomcgi.dev, dev.jomcgi.dev), and an
                 absolute production URL sent people off the environment they were
                 actually using. `/` redirects to /public/. -->
            <a href="/">← back to solid ground</a>
            <a href="/app/notes">talk to my notes</a>
          </div>
        </article>
      </div>
    </div>
  </div>
</main>

<style>
  .error-page {
    display: flex;
    align-items: center;
  }

  .error-page .frame {
    width: 100%;
  }

  .ed-lead h1 {
    font-size: clamp(1.5rem, 3vw, 2rem);
  }

  .path {
    font-family: var(--font-code);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }

  @media (max-width: 760px) {
    .error-page {
      display: block;
    }
  }
</style>
