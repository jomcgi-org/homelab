<script>
  import TechnicalDrawingChrome from "../components/TechnicalDrawingChrome.svelte";
  import Trail from "../components/Trail.svelte";
  import SiteIndex from "../components/SiteIndex.svelte";
  import "../styles/technical-drawing.css";

  let { current = "", sections = [], children } = $props();
</script>

{#snippet trail()}
  <Trail crumbs={[{ label: "ember", href: "/ember" }]} {current} />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet ember-sheet">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        {#if sections.length}
          <nav class="index strip" aria-label="On this page">
            <p class="sec-label">/ Index</p>
            <div class="index-list">
              {#each sections as section}
                <a href={`#${section.id}`}>{section.label}</a>
              {/each}
            </div>
          </nav>
        {/if}
        <SiteIndex />
      </aside>
      <div class="content editions">{@render children()}</div>
    </div>
  </div>
</main>
