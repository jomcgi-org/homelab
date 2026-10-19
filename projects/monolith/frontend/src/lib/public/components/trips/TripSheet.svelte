<script>
  import TechnicalDrawingChrome from "../TechnicalDrawingChrome.svelte";
  import Trail from "../Trail.svelte";
  import SiteIndex from "../SiteIndex.svelte";
  import "$lib/public/styles/technical-drawing.css";

  // Presentation only. Route data, photo selection and map lifetimes stay with
  // their existing pages. The short trail row can wrap titles below it.
  let { trip, current = "", children } = $props();
  const crumbs = $derived([
    { label: "trips", ...(trip ? { href: "/app/trips" } : {}) },
  ]);
</script>

{#snippet trail()}
  <Trail {crumbs} current={current || trip?.short_title || trip?.slug || ""} />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet trips-sheet">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        {#if trip && current}
          <nav class="index" aria-label="Trip">
            <div class="index-list">
              <a href={`/app/trips/${trip.slug}`}
                >{trip.short_title ?? trip.slug}</a
              >
            </div>
          </nav>
        {/if}
        <SiteIndex here="trips" />
      </aside>
      <div class="content">{@render children()}</div>
    </div>
  </div>
</main>

<style>
  .trips-sheet :global(h1),
  .trips-sheet :global(h2) {
    font-family: var(--font-ui);
    letter-spacing: -0.025em;
  }
  .trips-sheet :global(button),
  .trips-sheet :global(a) {
    overflow-wrap: anywhere;
  }
  .trips-sheet :global(.maplibregl-ctrl-group) {
    border: 1px solid var(--stroke);
    border-radius: 0;
    box-shadow: none;
    background: var(--sheet);
  }
  .trips-sheet :global(.maplibregl-ctrl-group button) {
    width: 44px;
    height: 44px;
    background: var(--sheet);
    color: var(--ink);
  }
  .trips-sheet :global(.maplibregl-ctrl-group button + button) {
    border-top: 1px solid var(--stroke);
  }
  /* MapLibre's stock icons are black bitmaps. Text glyphs let the sheet's ink
     paint them in either scheme without filtering the map or photographs. */
  .trips-sheet :global(.maplibregl-ctrl-zoom-in .maplibregl-ctrl-icon),
  .trips-sheet :global(.maplibregl-ctrl-zoom-out .maplibregl-ctrl-icon) {
    background: none;
    display: grid;
    place-items: center;
  }
  .trips-sheet :global(.maplibregl-ctrl-zoom-in .maplibregl-ctrl-icon::after) {
    content: "+";
  }
  .trips-sheet :global(.maplibregl-ctrl-zoom-out .maplibregl-ctrl-icon::after) {
    content: "−";
  }
</style>
