<script>
  // Contact-sheet grid of a day's photos. Clicking a tile calls onOpen(i); the
  // lightbox itself is owned by the page so the map and grid share one viewer.
  // Each photo carries a pre-signed `imgGallery` URL (signed server-side in the
  // trip [slug] layout load); this component never builds an imgproxy URL.
  let { photos = [], onOpen = () => {} } = $props();
</script>

{#if photos.length}
  <div class="grid">
    {#each photos as photo, i (photo.id)}
      <button
        class="tile"
        onclick={() => onOpen(i)}
        aria-label={`Open photo ${i + 1}`}
      >
        <img
          src={photo.imgGallery}
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
