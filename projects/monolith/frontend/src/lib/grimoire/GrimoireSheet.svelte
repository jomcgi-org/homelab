<script>
  import "@homelab/design-system/tokens/contract.css";
  import "$lib/public/styles/technical-drawing.css";
  import "$lib/grimoire/theme.css";
  import "$lib/grimoire/sheet.css";
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";

  let { current, children } = $props();
</script>

{#snippet trail()}
  <Trail root={{ label: "Grimoire", href: "/grimoire" }} {current} />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet grimoire grimoire-sheet">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <nav class="index site-index" aria-label="Account">
          <div class="index-list">
            <a href="/grimoire">Your campaigns</a>
            <a href="/grimoire/sheets">Character sheets</a>
            <a href="/grimoire/oauth2/logout">Sign out</a>
          </div>
        </nav>
      </aside>
      <div class="content">{@render children()}</div>
    </div>
  </div>
</main>
