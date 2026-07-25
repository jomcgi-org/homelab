<script>
  /**
   * Infinite scrolling ticker bar.
   * Animation duration scales with item count so the scroll velocity stays
   * roughly constant — otherwise a short item list with the same fixed
   * duration ends up moving a tiny distance per second (looks "slow").
   * @type {{ items: string[] }}
   */
  let { items } = $props();
  // ~4s per item, with a 16s floor so very short lists don't blur past.
  const duration = `${Math.max(16, items.length * 4)}s`;
</script>

<div class="marquee" aria-hidden="true">
  <div class="marquee-track" style="animation-duration: {duration};">
    {#each { length: 3 } as _}
      {#each items as item}
        <span class="marquee-item"><span class="marquee-dot"></span>{item}</span
        >
      {/each}
    {/each}
  </div>
</div>

<style>
  .marquee {
    background: var(--accent);
    color: var(--ink);
    border-bottom: 2px solid var(--ink);
    overflow: hidden;
    font-family: var(--mono);
    font-size: 13px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .marquee-track {
    display: flex;
    gap: 48px;
    padding: 10px 0;
    width: max-content;
    animation: marquee 48s linear infinite;
  }

  .marquee-item {
    display: inline-flex;
    align-items: center;
    gap: 14px;
    white-space: nowrap;
  }

  .marquee-dot {
    width: 7px;
    height: 7px;
    border-radius: 999px;
    background: var(--ink);
    display: inline-block;
    flex-shrink: 0;
  }

  @keyframes marquee {
    to {
      transform: translateX(-33.333%);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .marquee-track {
      animation: none;
    }
  }
</style>
