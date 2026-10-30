<script>
  let { status } = $props();
</script>

<section class="edition access-panel" aria-live="polite">
  <div class="ed-head">Crossing</div>
  <div class="ed-lead">
    {#if status === "forbidden"}
      <h1>This account is not recognised</h1>
      <p>Your signed-in account is not listed as a Crossing viewer.</p>
    {:else}
      <h1>The plan is unavailable</h1>
      <p>Crossing could not load the plan. Try again shortly.</p>
    {/if}
  </div>
</section>
