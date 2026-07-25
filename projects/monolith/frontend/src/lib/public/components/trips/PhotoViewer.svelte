<script>
  import { onMount } from "svelte";

  // Each photo carries a pre-signed `imgDisplay` URL (signed server-side in the
  // trip [slug] layout load); this component never builds an imgproxy URL.
  // Fullscreen lightbox for a list of photo points. Controlled: the parent owns
  // `index` and reacts to onIndex / onClose. Keyboard: left/right navigate,
  // Escape closes.
  let { photos = [], index = 0, tz = "UTC", onClose, onIndex } = $props();

  const photo = $derived(photos[index] ?? null);
  const hasPrev = $derived(index > 0);
  const hasNext = $derived(index < photos.length - 1);

  function prev() {
    if (hasPrev) onIndex?.(index - 1);
  }
  function next() {
    if (hasNext) onIndex?.(index + 1);
  }

  function fmtTime(iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString("en-CA", {
        timeZone: tz,
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return "";
    }
  }

  onMount(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
      else if (e.key === "ArrowLeft") prev();
      else if (e.key === "ArrowRight") next();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });
</script>

{#if photo}
  <div
    class="overlay"
    role="dialog"
    aria-modal="true"
    aria-label="Photo viewer"
  >
    <button class="backdrop" onclick={onClose} aria-label="Close photo viewer"
    ></button>
    <button class="close" onclick={onClose} aria-label="Close">&times;</button>

    {#if hasPrev}
      <button class="nav prev" onclick={prev} aria-label="Previous photo"
        >&larr;</button
      >
    {/if}

    <figure class="frame">
      <img
        src={photo.imgDisplay}
        alt={`Photo ${index + 1} of ${photos.length}`}
      />
      <figcaption>
        <span class="count">{index + 1} / {photos.length}</span>
        {#if photo.taken_at}<span>{fmtTime(photo.taken_at)}</span>{/if}
        {#if photo.focal_length_35mm}<span>{photo.focal_length_35mm}mm</span
          >{/if}
        {#if photo.aperture}<span>&fnof;/{photo.aperture}</span>{/if}
        {#if photo.iso}<span>ISO {photo.iso}</span>{/if}
        {#if photo.shutter_speed}<span>{photo.shutter_speed}</span>{/if}
        {#if photo.elevation != null}<span>{Math.round(photo.elevation)}m</span
          >{/if}
      </figcaption>
    </figure>

    {#if hasNext}
      <button class="nav next" onclick={next} aria-label="Next photo"
        >&rarr;</button
      >
    {/if}
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 50;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background: rgba(0, 0, 0, 0.92);
  }
  .backdrop {
    position: absolute;
    inset: 0;
    border: none;
    background: transparent;
    cursor: zoom-out;
  }
  .frame {
    position: relative;
    margin: 0;
    max-width: min(1200px, 100%);
    max-height: 100%;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .frame img {
    max-width: 100%;
    max-height: calc(100vh - 120px);
    object-fit: contain;
    border: 2px solid var(--paper);
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    color: rgba(255, 255, 255, 0.84);
  }
  .count {
    font-weight: 700;
    color: var(--paper);
  }
  .close {
    position: absolute;
    top: 16px;
    right: 20px;
    background: none;
    border: none;
    color: var(--paper);
    font-family: var(--mono);
    font-size: 34px;
    line-height: 1;
    cursor: pointer;
  }
  .nav {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    background: var(--paper);
    border: 2px solid var(--ink);
    color: var(--ink);
    font-family: var(--mono);
    font-size: 20px;
    font-weight: 700;
    padding: 10px 16px;
    cursor: pointer;
  }
  .prev {
    left: 16px;
  }
  .next {
    right: 16px;
  }
  .nav:hover {
    background: var(--ink);
    color: var(--paper);
  }
  @media (max-width: 640px) {
    .nav {
      padding: 8px 12px;
      font-size: 16px;
    }
  }
</style>
