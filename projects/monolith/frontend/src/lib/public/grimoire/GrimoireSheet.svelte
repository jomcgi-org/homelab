<script>
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import SiteIndex from "$lib/public/components/SiteIndex.svelte";
  import "$lib/public/styles/technical-drawing.css";
  import "./sheet.css";
  let { section = "home", immersive = false, children } = $props();
  const links = [
    { id: "library", label: "Library" },
    { id: "world", label: "World" },
    { id: "chat", label: "Chat" },
  ];
</script>

{#snippet trail()}
  <Trail
    crumbs={[
      { label: "Grimoire", href: "/app/grimoire" },
      ...(section === "home" ? [] : [{ label: section }]),
    ]}
  />
{/snippet}
<TechnicalDrawingChrome {trail} />
<main class="td sheet grimoire grimoire-sheet grimoire-app" class:immersive>
  <div class="frame">
    <div class="journal" class:single={immersive}>
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <nav class="index strip" aria-label="Grimoire sections">
          <p class="sec-label">/ Index</p>
          <div class="index-list">
            {#each links as link}
              <a
                href={`/app/grimoire/${link.id}`}
                class:active={section === link.id}
                aria-current={section === link.id ? "page" : undefined}
                >{link.label}</a
              >
            {/each}
          </div>
        </nav>
        <SiteIndex here="" />
      </aside>
      {#if !immersive}<div class="content editions">
          {@render children()}
        </div>{/if}
    </div>
  </div>
  {#if immersive}{@render children()}{/if}
</main>
