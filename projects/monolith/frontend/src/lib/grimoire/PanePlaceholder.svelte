<script>
  // The desktop right pane when nothing is selected yet (the list lives in the
  // left pane). Never shown on mobile, where list and detail are separate
  // screens.
  let { lead, help } = $props();
</script>

<div class="placeholder">
  <p class="lead grim-title">{lead}</p>
  <p class="help">{help}</p>
</div>

<style>
  .placeholder {
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
    padding: 2rem;
    text-align: center;
    color: var(--fg-tertiary);
  }

  .lead {
    font-size: 1.35rem;
    color: var(--grim-accent);
  }

  .help {
    font-size: 0.82rem;
    max-width: 24rem;
    color: var(--fg-secondary);
  }
</style>
