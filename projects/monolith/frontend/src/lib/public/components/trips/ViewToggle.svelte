<script>
  import { Button } from "@homelab/design-system/components";
  // Two-state segmented toggle (e.g. Map / Photos). `value` is bindable; options
  // is [{ value, label }, ...].
  let { value = $bindable(), options = [] } = $props();
</script>

<div class="toggle" role="group" aria-label="View">
  {#each options as opt (opt.value)}
    <Button
      type="button"
      variant={value === opt.value ? "primary" : "secondary"}
      aria-pressed={value === opt.value}
      onclick={() => (value = opt.value)}
    >
      {opt.label}
    </Button>
  {/each}
</div>

<style>
  .toggle {
    display: flex;
    flex-wrap: wrap;
    gap: 1px;
    font: 0.75rem var(--font-code);
  }
</style>
