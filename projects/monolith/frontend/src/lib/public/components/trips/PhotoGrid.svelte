<script>
  import { onDestroy } from "svelte";

  // Contact-sheet grid of a day's photos. Clicking a tile calls onOpen(i); the
  // lightbox itself is owned by the page so the map and grid share one viewer.
  // Each photo carries a pre-signed `imgGallery` URL (signed server-side in the
  // trip [slug] layout load); this component never builds an imgproxy URL.
  let { photos = [], onOpen = () => {} } = $props();

  // Six images cover the widest row in the sheet and three mobile rows. Keep
  // their real URLs in SSR; native lazy lookahead otherwise queues dozens of
  // later photos before those first photos have finished on a slow connection.
  const INITIAL_PHOTOS = 6;
  // Track each URL separately so revealing a few tiles does not invalidate
  // every conditional in a gallery containing thousands of photos.
  const readyUrls = $state({});
  let nativeLazy = $state(false);
  let observer;
  const pending = new Map();

  function reveal(urls) {
    for (const url of urls) readyUrls[url] = true;
  }

  function observePhoto(node, value) {
    function update({ url, eager }) {
      observer?.unobserve(node);
      pending.delete(node);
      if (eager || readyUrls[url] === true || nativeLazy) return;
      if (typeof IntersectionObserver !== "function") {
        nativeLazy = true;
        return;
      }
      observer ??= new IntersectionObserver(
        (entries) => {
          const urls = [];
          for (const entry of entries) {
            const url = pending.get(entry.target);
            if (!entry.isIntersecting || !url) continue;
            urls.push(url);
            pending.delete(entry.target);
            observer.unobserve(entry.target);
          }
          reveal(urls);
        },
        { rootMargin: "160px 0px" },
      );
      pending.set(node, url);
      observer.observe(node);
    }
    update(value);
    return {
      update,
      destroy() {
        pending.delete(node);
        observer?.unobserve(node);
      },
    };
  }

  onDestroy(() => {
    observer?.disconnect();
    pending.clear();
  });
</script>

{#if photos.length}
  <div class="grid">
    {#each photos as photo, i (photo.id)}
      <button
        class="tile"
        onclick={() => onOpen(i)}
        onfocus={() => reveal([photo.imgGallery])}
        aria-label={`Open photo ${i + 1}`}
        use:observePhoto={{ url: photo.imgGallery, eager: i < INITIAL_PHOTOS }}
      >
        {#if i < INITIAL_PHOTOS || readyUrls[photo.imgGallery] === true || nativeLazy}
          <img
            src={photo.imgGallery}
            alt={`Photo ${i + 1}`}
            loading={i < INITIAL_PHOTOS || !nativeLazy ? "eager" : "lazy"}
            fetchpriority={i < INITIAL_PHOTOS ? "high" : "auto"}
            decoding="async"
          />
        {:else}
          <noscript>
            <img
              src={photo.imgGallery}
              alt={`Photo ${i + 1}`}
              loading="lazy"
              decoding="async"
            />
          </noscript>
        {/if}
      </button>
    {/each}
  </div>
{:else}
  <p class="empty">No photos for this day.</p>
{/if}

<style>
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(min(100%, 140px), 1fr));
    gap: 1px;
    background: var(--stroke);
    border: 1px solid var(--ink);
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
  }
  .tile:hover img,
  .tile:focus-visible img {
    opacity: 0.75;
  }
  .empty {
    font-family: var(--font-code);
    font-size: 12px;
    color: var(--ink-2);
  }
</style>
