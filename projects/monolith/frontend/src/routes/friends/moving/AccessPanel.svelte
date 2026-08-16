<script>
  let { status } = $props();
</script>

<section class="access-panel" aria-live="polite">
  <p class="eyebrow">Crossing</p>
  {#if status === "forbidden"}
    <h1>This account is not recognised</h1>
    <p>Your signed-in account is not listed as a Crossing viewer.</p>
  {:else}
    <h1>The plan is unavailable</h1>
    <p>Crossing could not load the plan. Try again shortly.</p>
  {/if}
</section>

<style>
  .access-panel {
    width: min(34rem, calc(100% - 2rem));
    padding: clamp(1.5rem, 5vw, 3rem);
    background: var(--moving-paper);
    border: var(--moving-border) solid var(--moving-ink);
  }

  .eyebrow {
    margin: 0 0 1rem;
    color: var(--moving-muted);
    font-family: var(--moving-font-mono);
    font-size: var(--moving-size-small);
    font-weight: 800;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  h1 {
    margin: 0;
    font-size: var(--moving-size-heading);
    line-height: 0.95;
  }

  p:last-child {
    max-width: 30rem;
    margin: 1rem 0 0;
    font-size: var(--moving-size-body);
    line-height: 1.5;
  }
</style>
