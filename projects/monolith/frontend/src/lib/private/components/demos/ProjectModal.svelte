<script>
  // Rich modal shell for a firecracker demo project. Owns the
  // backdrop/dialog chrome (open/close, escape, backdrop click) and
  // renders whatever is passed as children — RunPanel, in practice.
  //
  // Props:
  //   project — { key, label, tagline, accent } | null. Modal is shown
  //             when non-null.
  //   onClose — () => void
  import { fade, scale } from "svelte/transition";
  import { quintOut } from "svelte/easing";

  let { project, onClose, children } = $props();

  let dialogRef = $state(null);

  $effect(() => {
    if (!project) return;
    dialogRef?.focus();
    function onKeydown(e) {
      if (e.key === "Escape") onClose?.();
    }
    document.addEventListener("keydown", onKeydown);
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKeydown);
      document.body.style.overflow = prevOverflow;
    };
  });
</script>

{#if project}
  <div
    class="backdrop"
    transition:fade={{ duration: 140 }}
    onclick={(e) => {
      if (e.target === e.currentTarget) onClose?.();
    }}
  >
    <div
      class="dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="project-modal-title"
      tabindex="-1"
      bind:this={dialogRef}
      transition:scale={{ duration: 160, start: 0.97, easing: quintOut }}
    >
      <header class="dialog-header" style={`--accent-swatch: ${project.accent}`}>
        <div class="dialog-heading">
          <span class="dialog-swatch" aria-hidden="true"></span>
          <div>
            <h2 id="project-modal-title" class="dialog-title">
              {project.label}
            </h2>
            <p class="dialog-tagline">{project.tagline}</p>
          </div>
        </div>
        <button
          type="button"
          class="dialog-close"
          aria-label="Close"
          onclick={() => onClose?.()}
        >
          &times;
        </button>
      </header>

      <div class="dialog-body">
        {@render children?.()}
      </div>
    </div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 2rem 1.25rem;
    z-index: 300;
    overflow-y: auto;
  }

  .dialog {
    width: min(46rem, 100%);
    max-height: calc(100vh - 4rem);
    display: flex;
    flex-direction: column;
    background: var(--bg);
    color: var(--fg);
    border: var(--border-heavy);
    box-shadow: 8px 8px 0 0 rgba(0, 0, 0, 0.35);
    font-family: var(--font-mono);
  }

  .dialog-header {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    padding: 1.25rem 1.5rem;
    border-bottom: var(--border-heavy);
    flex-shrink: 0;
  }

  .dialog-heading {
    display: flex;
    align-items: flex-start;
    gap: 0.75rem;
    min-width: 0;
  }

  .dialog-swatch {
    width: 0.85rem;
    height: 0.85rem;
    margin-top: 0.3rem;
    flex-shrink: 0;
    background: var(--accent-swatch, var(--accent));
    border: 1px solid var(--fg);
  }

  .dialog-title {
    font-size: 1.15rem;
    font-weight: 700;
    letter-spacing: -0.01em;
    margin: 0;
  }

  .dialog-tagline {
    font-size: 0.8rem;
    color: var(--fg-secondary);
    margin: 0.2rem 0 0 0;
  }

  .dialog-close {
    background: transparent;
    border: 2px solid transparent;
    color: var(--fg);
    font-size: 1.4rem;
    line-height: 1;
    cursor: pointer;
    padding: 0.1rem 0.5rem;
    flex-shrink: 0;
    transition:
      border-color 0.1s ease,
      transform 0.1s ease;
  }

  .dialog-close:hover {
    border-color: var(--fg);
    transform: translate(-1px, -1px);
  }

  .dialog-close:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  .dialog-body {
    padding: 1.5rem;
    overflow-y: auto;
  }

  @media (max-width: 640px) {
    .backdrop {
      padding: 0;
      align-items: stretch;
    }
    .dialog {
      width: 100%;
      max-height: 100vh;
      border-width: 0 0 0 0;
      border-left: none;
      border-right: none;
    }
  }
</style>
