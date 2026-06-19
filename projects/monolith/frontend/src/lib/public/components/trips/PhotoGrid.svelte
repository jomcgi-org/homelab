<script>
  import { imgUrl } from "$lib/trips/images.js";
  import PhotoViewer from "./PhotoViewer.svelte";

  // Contact-sheet grid of a day's photos. Clicking a tile opens the lightbox.
  // Self-contained: owns the lightbox open/index state.
  let { photos = [], tz = "UTC" } = $props();

  let open = $state(false);
  let index = $state(0);

  function show(i) {
    index = i;
    open = true;
  }
</script>

{#if photos.length}
  <div class="grid">
    {#each photos as photo, i (photo.id)}
      <button class="tile" onclick={() => show(i)} aria-label={`Open photo ${i + 1}`}>
        <img
          src={imgUrl(photo.image, "gallery")}
          alt={`Photo ${i + 1}`}
          loading="lazy"
          decoding="async"
        />
      </button>
    {/each}
  </div>
{:else}
  <p class="empty">No photos for this day.</p>
{/if}

{#if open}
  <PhotoViewer
    {photos}
    {index}
    {tz}
    onIndex={(i) => (index = i)}
    onClose={() => (open = false)}
  />
{/if}

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 2px;
    border: 2px solid var(--ink);
  }
  .tile {
    position: relative;
    aspect-ratio: 1;
    padding: 0;
    border: none;
    background: var(--ink);
    cursor: pointer;
    overflow: hidden;
  }
  .tile img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
    transition: opacity 150ms ease;
  }
  .tile:hover img,
  .tile:focus-visible img {
    opacity: 0.75;
  }
  .empty {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
  }
</style>
