<script>
  import Footer from "./Footer.svelte";
  import SiteIndex from "./SiteIndex.svelte";
  import TechnicalDrawingChrome from "./TechnicalDrawingChrome.svelte";
  import Trail from "./Trail.svelte";
  import "$lib/public/styles/technical-drawing.css";

  // Only home and CV use this shell. Legacy routes keep their own chrome and
  // the public layout keeps the stylesheet they still depend on.
  let { here = "", sections = [], children } = $props();
</script>

{#snippet trail()}
  <Trail crumbs={here === "cv" ? [{ label: "cv" }] : []} />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet profile-sheet">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <nav class="index strip" aria-label="On this page">
          <p class="sec-label">/ Index</p>
          <div class="index-list">
            {#each sections as section}
              <a href={`#${section.id}`}>{section.label}</a>
            {/each}
          </div>
        </nav>
        <SiteIndex {here} />
      </aside>
      <div class="content editions">
        {@render children()}
        <Footer />
      </div>
    </div>
  </div>
</main>
