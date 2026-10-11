<script>
  // Players receive the server's projection already, but a hidden entry is
  // masked again here so a secret label can never reach a player's DOM even
  // if a caller passes the DM view.
  let { entries = [], round = 1, activeIndex = null, dm = false } = $props();
</script>

{#if entries.length}
  <section class="turn-strip" aria-label="Turn order">
    <p class="round">Round {round}</p>
    <ol>
      {#each entries as entry, index}
        {@const active = index === activeIndex}
        {@const concealed = entry.hidden && !dm}
        <li class:active aria-current={active ? "step" : undefined}>
          <span class="name">{concealed ? "???" : entry.label}</span>
          {#if entry.hidden && dm}<span class="badge">hidden</span>{/if}
          {#if active}<span class="marker">Active turn</span>{/if}
        </li>
      {/each}
    </ol>
  </section>
{/if}

<style>
  .turn-strip {
    margin: 0 0 24px;
    padding: 14px 16px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
  }
  .round {
    margin: 0 0 10px;
    font-size: 13px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--grim-ink-soft);
  }
  ol {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 0;
    padding: 0;
    list-style: none;
  }
  li {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    padding: 8px 12px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface-2);
    color: var(--grim-ink);
  }
  li.active {
    border: 2px solid var(--grim-accent);
    background: var(--grim-accent-soft);
    font-weight: 600;
  }
  .badge,
  .marker {
    font-size: 12px;
    padding: 2px 6px;
    border: 1px solid var(--grim-ink-soft);
    color: var(--grim-ink-soft);
  }
  .marker {
    border-color: var(--grim-accent);
    color: var(--grim-accent-strong);
  }
</style>
