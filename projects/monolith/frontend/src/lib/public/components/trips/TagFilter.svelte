<script>
  import { Button } from "@homelab/design-system/components";
  // Tag toggle controls. `selected` is a bindable array of active (lowercased) tags;
  // clicking a tag adds/removes it.
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
      <Button
        type="button"
        class="chip"
        variant={selected.includes(tag.toLowerCase()) ? "primary" : "secondary"}
        aria-pressed={selected.includes(tag.toLowerCase())}
        onclick={() => toggle(tag)}
      >
        {tag}
      </Button>
    {/each}
  </div>
{/if}

<style>
  .tagfilter {
    display: flex;
    flex-wrap: wrap;
    gap: 1px;
    font: 0.75rem var(--font-code);
  }
</style>
