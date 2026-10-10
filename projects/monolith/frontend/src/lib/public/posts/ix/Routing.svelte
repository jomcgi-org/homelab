<script>
  import Fig from "./Fig.svelte";
  import { EXPERTS, routedExperts } from "./data-routing.js";

  let token = $state(1);
  const routed = $derived(new Set(routedExperts(token)));
  const cells = Array.from({ length: EXPERTS }, (_, i) => i);
</script>

<Fig title="Example layers with random expert distribution">
  {#snippet controls()}
    <button type="button" class="next" onclick={() => (token += 1)}
      >Next token</button
    >
    <span class="ix-label">Token {token}</span>
  {/snippet}

  <div class="pool" aria-hidden="true">
    {#each cells as i}<i class:on={routed.has(i)}></i>{/each}
  </div>
  <div class="shared" aria-hidden="true">shared expert</div>
  <div class="readout" aria-live="polite">
    10 of 512 routed experts in this layer, plus the shared expert. Positions
    are illustrative.
  </div>
</Fig>

<style>
  .next {
    min-height: 2.5rem;
    padding: 0.35rem 0.75rem;
    border: 1px solid var(--ink);
    background: var(--ink);
    color: var(--sheet);
    font: 0.72rem var(--font-code);
    cursor: pointer;
  }
  .pool {
    display: grid;
    grid-template-columns: repeat(32, minmax(0, 1fr));
    gap: 2px;
  }
  .pool i {
    aspect-ratio: 1;
    border: 1px solid var(--line);
    transition: background-color 160ms ease;
  }
  .pool i.on {
    border-color: var(--tone-hot);
    background: var(--tone-hot);
  }
  .shared {
    margin-top: 0.5rem;
    padding: 0.3rem 0.5rem;
    border: 1px solid var(--tone-gpu);
    background: color-mix(in srgb, var(--tone-gpu) 12%, var(--sheet));
    color: var(--ink);
    font: 0.68rem var(--font-code);
  }
  .readout {
    margin-top: 0.6rem;
    color: var(--ink-2);
    font: 0.72rem / 1.5 var(--font-code);
  }
  @media (prefers-reduced-motion: reduce) {
    .pool i {
      transition: none;
    }
  }
</style>
