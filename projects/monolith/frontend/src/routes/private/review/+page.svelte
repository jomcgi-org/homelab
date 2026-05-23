<script>
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";

  let { data } = $props();

  // Card index within the current queue. Reset on tab/mode change.
  let index = $state(0);
  let current = $derived(data.items[index]);

  function setTab(t) {
    const url = new URL($page.url);
    url.searchParams.set("tab", t);
    index = 0;
    goto(url, { replaceState: true, invalidateAll: true });
  }

  function setMode(m) {
    const url = new URL($page.url);
    url.searchParams.set("mode", m);
    index = 0;
    goto(url, { replaceState: true, invalidateAll: true });
  }
</script>

<svelte:head><title>Review · private.jomcgi.dev</title></svelte:head>

<section class="review">
  <header class="bar">
    <div class="tabs" role="tablist" aria-label="Review tab">
      <button
        role="tab"
        aria-selected={data.tab === "gaps"}
        class:active={data.tab === "gaps"}
        onclick={() => setTab("gaps")}
      >
        Gaps
      </button>
      <button
        role="tab"
        aria-selected={data.tab === "notes"}
        class:active={data.tab === "notes"}
        onclick={() => setTab("notes")}
      >
        Notes
      </button>
    </div>

    <!-- TODO Task 5: replace inline mode buttons with <ModeToggle {mode} on:change={...} /> -->
    <div class="modes" role="tablist" aria-label="Review mode">
      <button
        role="tab"
        aria-selected={data.mode === "pending"}
        class:active={data.mode === "pending"}
        onclick={() => setMode("pending")}
      >
        Pending
      </button>
      <button
        role="tab"
        aria-selected={data.mode === "audit"}
        class:active={data.mode === "audit"}
        onclick={() => setMode("audit")}
      >
        Audit auto-decisions
      </button>
    </div>
  </header>

  {#if data.error}
    <p class="error">{data.error}</p>
  {:else if !current}
    <p class="empty">Queue empty for {data.tab} / {data.mode}.</p>
  {:else}
    <!-- TODO Task 5: replace with <ReviewCard item={current} tab={data.tab} mode={data.mode} on:decide={(e) => decide(e.detail, current)} /> -->
    <article class="card-placeholder">
      <h2>{data.tab === "gaps" ? current.term : current.title}</h2>
      <pre>{JSON.stringify(current, null, 2)}</pre>
    </article>
    <footer class="counter">{index + 1} / {data.items.length}</footer>
  {/if}
</section>

<style>
  .review {
    padding: 2rem 2.5rem;
    font-family: var(--font);
    color: var(--fg);
    background: var(--bg);
    min-height: calc(100vh - 4rem);
  }

  .bar {
    display: flex;
    gap: 2rem;
    align-items: center;
    margin-bottom: 1.5rem;
    padding-bottom: 0.75rem;
    border-bottom: 0.04rem solid var(--border);
  }

  .tabs,
  .modes {
    display: inline-flex;
    gap: 0.25rem;
  }

  .tabs button,
  .modes button {
    font-family: var(--font);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    background: transparent;
    border: none;
    padding: 0.4rem 0.6rem;
    cursor: pointer;
  }

  .tabs button.active,
  .modes button.active {
    color: var(--fg);
  }

  .card-placeholder {
    border: 0.04rem solid var(--border);
    padding: 1rem 1.25rem;
    border-radius: 4px;
    background: var(--surface, transparent);
  }

  .card-placeholder h2 {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    font-weight: 700;
  }

  .card-placeholder pre {
    font-family: var(--font);
    font-size: 0.8rem;
    line-height: 1.5;
    color: var(--fg-secondary);
    white-space: pre-wrap;
    word-break: break-word;
    margin: 0;
  }

  .counter {
    font-size: 0.75rem;
    color: var(--fg-tertiary);
    letter-spacing: 0.04em;
    margin-top: 1rem;
    font-variant-numeric: tabular-nums;
  }

  .error {
    color: var(--danger);
    font-size: 0.85rem;
  }

  .empty {
    color: var(--fg-tertiary);
    font-size: 0.85rem;
  }
</style>
