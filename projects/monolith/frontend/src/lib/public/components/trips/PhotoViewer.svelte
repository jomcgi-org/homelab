<script>
  import { onMount } from "svelte";

  // Each photo carries a pre-signed `imgDisplay` URL (signed server-side in the
  // trip [slug] layout load); this component never builds an imgproxy URL.
  // Fullscreen lightbox for a list of photo points. Controlled: the parent owns
  // `index` and reacts to onIndex / onClose. Keyboard: left/right navigate,
  // Escape closes.
  let {
    photos = [],
    index = 0,
    tz = "UTC",
    onClose,
    onIndex,
    focusFallback,
  } = $props();
  let dialog = $state();
  let closeButton = $state();

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
    const opener = document.activeElement;
    closeButton?.focus();
    const containFocus = (e) => {
      if (dialog && !dialog.contains(e.target)) closeButton?.focus();
    };
    const onKey = (e) => {
      if (["Escape", "ArrowLeft", "ArrowRight"].includes(e.key)) {
        e.preventDefault();
        e.stopPropagation();
        if (e.key === "Escape") onClose?.();
        else if (e.key === "ArrowLeft") prev();
        else next();
      } else if (e.key === "Tab") {
        const buttons = [...dialog.querySelectorAll("button:not(:disabled)")];
        const first = buttons[0];
        const last = buttons.at(-1);
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last?.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first?.focus();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    document.addEventListener("focusin", containFocus);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.removeEventListener("focusin", containFocus);
      // Let the parent remove filtered tiles before choosing the return target.
      queueMicrotask(() => {
        if (opener?.isConnected && opener !== document.body) opener.focus();
        else if (focusFallback?.isConnected) focusFallback.focus();
      });
    };
  });
</script>

{#if photo}
  <div
    bind:this={dialog}
    class="overlay"
    role="dialog"
    aria-modal="true"
    aria-label="Photo viewer"
  >
    <button class="backdrop" onclick={onClose} aria-label="Close photo viewer"
    ></button>
    <button
      bind:this={closeButton}
      class="close"
      onclick={onClose}
      aria-label="Close">&times;</button
    >

    <button
      class="nav prev"
      disabled={!hasPrev}
      onclick={prev}
      aria-label="Previous photo">&larr;</button
    >

    <figure class="photo-frame">
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

    <button
      class="nav next"
      disabled={!hasNext}
      onclick={next}
      aria-label="Next photo">&rarr;</button
    >
  </div>
{/if}

<style>
  .overlay {
    position: fixed;
    inset: 0;
    z-index: 1100;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
    background: var(--sheet);
  }
  .backdrop {
    position: absolute;
    inset: 0;
    border: none;
    background: transparent;
    cursor: zoom-out;
  }
  .photo-frame {
    position: relative;
    margin: 0;
    max-width: min(1200px, 100%);
    max-height: 100%;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .photo-frame img {
    max-width: 100%;
    max-height: calc(100vh - 120px);
    object-fit: contain;
    border: 1px solid var(--stroke);
  }
  figcaption {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    font-family: var(--font-code);
    font-size: 11px;
    letter-spacing: 0.04em;
    color: var(--ink-2);
  }
  .count {
    font-weight: 700;
    color: var(--ink);
  }
  .close {
    position: absolute;
    z-index: 1;
    top: 16px;
    right: 20px;
    background: none;
    border: none;
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 34px;
    line-height: 1;
    cursor: pointer;
  }
  .nav {
    position: absolute;
    z-index: 1;
    top: 50%;
    transform: translateY(-50%);
    background: var(--sheet);
    border: 1px solid var(--ink);
    color: var(--ink);
    font-family: var(--font-code);
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
    background: var(--band);
    color: var(--ink);
  }
  .nav:disabled {
    opacity: 0.5;
    cursor: default;
  }
  button:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  @media (max-width: 640px) {
    .nav {
      padding: 8px 12px;
      font-size: 16px;
    }
  }
</style>
