<script>
  // Inline "Deleted X · Undo" toast pinned to the bottom of the page.
  // Auto-dismisses after `durationMs`; clicking Undo (or the `u` key
  // when the toast is visible, wired in the page) fires `onUndo`.
  //
  // Props (Svelte 5 runes):
  //   label    — string shown to the left ("Deleted gap 'foo'")
  //   onUndo   — () => void, invoked when the user clicks Undo
  //   onDismiss — () => void, invoked on auto-timeout or X click
  //   durationMs — auto-dismiss delay (default 10s)
  let { label, onUndo, onDismiss, durationMs = 10_000 } = $props();

  // Restart the auto-dismiss timer whenever the label changes — useful
  // when the user deletes another item before the previous toast has
  // expired (the parent keys this component on the latest tombstone).
  $effect(() => {
    label;
    const id = setTimeout(() => onDismiss?.(), durationMs);
    return () => clearTimeout(id);
  });
</script>

<div class="toast" role="status" aria-live="polite">
  <span class="toast-label">{label}</span>
  <button type="button" class="toast-undo" onclick={() => onUndo?.()}>
    Undo (u)
  </button>
  <button
    type="button"
    class="toast-dismiss"
    aria-label="Dismiss"
    onclick={() => onDismiss?.()}>×</button
  >
</div>

<style>
  .toast {
    position: fixed;
    bottom: 1.25rem;
    left: 50%;
    transform: translateX(-50%);
    display: inline-flex;
    align-items: center;
    gap: 1rem;
    padding: 0.5rem 0.75rem 0.5rem 1rem;
    background: var(--bg);
    border: 0.06rem solid var(--border);
    border-radius: 4px;
    font-family: var(--font);
    font-size: 0.8rem;
    color: var(--fg);
    z-index: 200;
    max-width: min(36rem, calc(100vw - 2rem));
  }

  .toast-label {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .toast-undo {
    font-family: var(--font);
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    background: transparent;
    border: 0.04rem solid var(--border);
    padding: 0.3rem 0.6rem;
    border-radius: 2px;
    cursor: pointer;
  }

  .toast-undo:hover {
    background: var(--surface);
  }

  .toast-undo:focus-visible {
    outline: 1.5px solid var(--fg);
    outline-offset: 2px;
  }

  .toast-dismiss {
    background: transparent;
    border: none;
    color: var(--fg-tertiary);
    font-size: 1rem;
    line-height: 1;
    cursor: pointer;
    padding: 0 0.25rem;
  }

  .toast-dismiss:hover {
    color: var(--fg);
  }
</style>
