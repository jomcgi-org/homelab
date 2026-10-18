<script>
  // The region stays mounted so a screen reader announces the text when it
  // appears. Only an active entry that names one of the viewer's own
  // characters counts: a masked or omitted entry carries no character id.
  let { entries = [], activeIndex = null, characterIds = [] } = $props();
  let yours = $derived.by(() => {
    const active = activeIndex === null ? null : entries[activeIndex];
    const pc = active?.player_character_id;
    return Boolean(pc) && !active.hidden && characterIds.includes(pc);
  });
</script>

<div role="status" aria-live="polite" class="turn-banner-region">
  {#if yours}<p class="turn-banner"><strong>Your turn</strong></p>{/if}
</div>

<style>
  .turn-banner {
    margin: 0 0 16px;
    padding: 12px 16px;
    border: 2px solid var(--grim-accent);
    background: var(--grim-accent-soft);
    color: var(--grim-accent-strong);
    font-size: 18px;
  }
</style>
