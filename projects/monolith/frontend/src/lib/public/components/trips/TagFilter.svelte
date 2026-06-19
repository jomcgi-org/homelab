<script>
  // Tag toggle chips. `selected` is a bindable array of active (lowercased) tags;
  // clicking a chip adds/removes it.
  let { tags = [], selected = $bindable([]) } = $props();

  function toggle(tag) {
    const t = tag.toLowerCase();
    selected = selected.includes(t)
      ? selected.filter((x) => x !== t)
      : [...selected, t];
  }
</script>

{#if tags.length}
  <div class="tagfilter" role="group" aria-label="Filter photos by tag">
    {#each tags as tag (tag)}
      <button
        type="button"
        class="chip"
        class:active={selected.includes(tag.toLowerCase())}
        aria-pressed={selected.includes(tag.toLowerCase())}
        onclick={() => toggle(tag)}
      >
        {tag}
      </button>
    {/each}
  </div>
{/if}

<style>
  .tagfilter {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .chip {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    padding: 6px 10px;
    background: var(--paper);
    border: 2px solid var(--ink);
    color: var(--ink);
    cursor: pointer;
  }
  .chip.active {
    background: var(--ink);
    color: var(--paper);
  }
</style>
