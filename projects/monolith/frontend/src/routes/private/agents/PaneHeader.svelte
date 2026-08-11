<script>
  let { kind, crumbs = [], onCrumb = () => {}, chips } = $props();
</script>

{#if crumbs.length}
  <nav class="crumbs" aria-label="Location">
    {#each crumbs as crumb, i}
      {#if crumb.to}
        <button
          class="crumb-link"
          type="button"
          onclick={() => onCrumb(crumb.to)}>{crumb.label}</button
        >
      {:else}<span class="crumb-current" aria-current="page">{crumb.label}</span
        >{/if}
      {#if i < crumbs.length - 1}<span class="crumb-sep" aria-hidden="true"
          >/</span
        >{/if}
    {/each}
  </nav>
{/if}
<div class="kind-row">
  <span class="kind">{kind}</span>
  {@render chips?.()}
</div>
