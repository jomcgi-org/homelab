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
  /* A small panel at the foot of the sheet: the undo offer is a control
     row, drawn like any other. */
  .toast {
    position: fixed;
    bottom: 1.25rem;
    left: 50%;
    z-index: 200;
    display: inline-flex;
    align-items: center;
    gap: 1rem;
    max-width: min(36rem, calc(100vw - 2rem));
    padding: 0.5rem 0.75rem 0.5rem 1rem;
    border: 1px solid var(--ink);
    background: var(--sheet);
    color: var(--ink);
    font: 0.8rem var(--font-code);
    transform: translateX(-50%);
  }

  .toast-label {
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
  }

  .toast-undo {
    padding: 0.3rem 0.6rem;
    border: 1px solid var(--stroke);
    background: var(--band);
    color: var(--accent-ink);
    font: 0.75rem var(--font-code);
    cursor: pointer;
  }

  .toast-undo:hover {
    color: var(--ink);
  }

  .toast-dismiss {
    padding: 0 0.25rem;
    border: 0;
    background: none;
    color: var(--ink-2);
    font: 1rem var(--font-code);
    line-height: 1;
    cursor: pointer;
  }

  .toast-dismiss:hover {
    color: var(--ink);
  }
</style>
