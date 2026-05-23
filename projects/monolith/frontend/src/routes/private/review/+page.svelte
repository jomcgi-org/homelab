<script>
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";
  import ReviewCard from "$lib/private/components/ReviewCard.svelte";
  import ModeToggle from "$lib/private/components/ModeToggle.svelte";

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

  // TODO Task 6: wire `handleDecide` into the decide endpoints + keyboard
  // shortcuts. The callback receives the action ('yes' | 'no' | 'skip') and
  // has closure access to `data.tab`, `data.mode`, and `current` — Task 6
  // will use those to build the endpoint path and advance the index.
  function handleDecide(action) {
    console.log("decide", {
      tab: data.tab,
      mode: data.mode,
      action,
      itemId: current?.id,
    });
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

    <ModeToggle mode={data.mode} onChange={setMode} />
  </header>

  {#if data.error}
    <p class="error">{data.error}</p>
  {:else if !current}
    <p class="empty">Queue empty for {data.tab} / {data.mode}.</p>
  {:else}
    <ReviewCard
      item={current}
      tab={data.tab}
      mode={data.mode}
      onDecide={handleDecide}
    />
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

  .tabs {
    display: inline-flex;
    gap: 0.25rem;
  }

  .tabs button {
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

  .tabs button.active {
    color: var(--fg);
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
