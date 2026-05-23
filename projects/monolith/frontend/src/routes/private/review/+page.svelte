<script>
  import { page } from "$app/stores";
  import { goto, invalidateAll } from "$app/navigation";
  import { deserialize } from "$app/forms";
  import ReviewCard from "$lib/private/components/ReviewCard.svelte";
  import ModeToggle from "$lib/private/components/ModeToggle.svelte";

  let { data } = $props();

  // Card index within the current queue. Reset on tab/mode change or refetch.
  let index = $state(0);
  let pendingError = $state(null);

  let current = $derived(data.items[index]);

  // If the queue shrinks (e.g. after invalidateAll refetch with fewer items),
  // clamp the index so `current` doesn't go undefined while items remain.
  $effect(() => {
    if (data.items.length === 0) {
      if (index !== 0) index = 0;
      return;
    }
    if (index > data.items.length - 1) {
      index = 0;
    }
  });

  function setTab(t) {
    const url = new URL($page.url);
    url.searchParams.set("tab", t);
    index = 0;
    pendingError = null;
    goto(url, { replaceState: true, invalidateAll: true });
  }

  function setMode(m) {
    const url = new URL($page.url);
    url.searchParams.set("mode", m);
    index = 0;
    pendingError = null;
    goto(url, { replaceState: true, invalidateAll: true });
  }

  function endpointFor(tab, mode, action, item) {
    if (tab === "gaps") {
      if (mode === "pending") {
        return {
          path:
            action === "yes"
              ? `/api/knowledge/gaps/${item.id}/verify`
              : `/api/knowledge/gaps/${item.id}/reject`,
        };
      }
      return {
        path:
          action === "yes"
            ? `/api/knowledge/gaps/${item.id}/verify`
            : `/api/knowledge/gaps/${item.id}/reopen`,
      };
    }
    // notes
    if (mode === "pending") {
      const visibility = action === "yes" ? "public" : "private";
      return {
        path: `/api/knowledge/notes/${item.id}/visibility`,
        body: JSON.stringify({ visibility }),
      };
    }
    return {
      path:
        action === "yes"
          ? `/api/knowledge/notes/${item.id}/verify-visibility`
          : `/api/knowledge/notes/${item.id}/reset-visibility`,
    };
  }

  function advance() {
    if (index < data.items.length - 1) index++;
  }

  function back() {
    if (index > 0) index--;
  }

  async function handleDecide(action) {
    if (!current) return;
    if (action === "skip") {
      advance();
      return;
    }

    const { path, body } = endpointFor(data.tab, data.mode, action, current);

    // Optimistic UI: advance immediately, roll back on failure.
    const prevIndex = index;
    advance();

    const formData = new FormData();
    formData.set("path", path);
    const actionName = body ? "decideWithBody" : "decide";
    if (body) formData.set("body", body);

    try {
      const res = await fetch(`?/${actionName}`, {
        method: "POST",
        body: formData,
      });
      const result = deserialize(await res.text());
      if (result.type === "failure" || result.type === "error") {
        index = prevIndex;
        pendingError = result.data?.error ?? "Decide failed";
        return;
      }
      pendingError = null;
      // Refill the queue when we're near the end.
      if (data.items.length - index <= 3) {
        await invalidateAll();
        index = 0;
      }
    } catch (e) {
      index = prevIndex;
      pendingError = e?.message ?? String(e);
    }
  }

  function isTypingTarget(el) {
    if (!el || el === document.body) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    if (el.isContentEditable) return true;
    return false;
  }

  $effect(() => {
    const handler = (e) => {
      if (isTypingTarget(e.target)) return;
      switch (e.key) {
        case "j":
        case "ArrowDown":
          e.preventDefault();
          advance();
          break;
        case "k":
        case "ArrowUp":
          e.preventDefault();
          back();
          break;
        case "y":
          e.preventDefault();
          handleDecide("yes");
          break;
        case "n":
          e.preventDefault();
          handleDecide("no");
          break;
        case "s":
          if (data.mode === "audit") {
            e.preventDefault();
            handleDecide("skip");
          }
          break;
        case "Tab":
          e.preventDefault();
          setTab(data.tab === "gaps" ? "notes" : "gaps");
          break;
        case "m":
          e.preventDefault();
          setMode(data.mode === "pending" ? "audit" : "pending");
          break;
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  });
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

    <div class="legend" aria-hidden="true">
      <kbd>j</kbd>/<kbd>k</kbd> nav
      · <kbd>y</kbd>/<kbd>n</kbd> decide
      {#if data.mode === "audit"}
        · <kbd>s</kbd> skip
      {/if}
      · <kbd>Tab</kbd> tab
      · <kbd>m</kbd> mode
    </div>
  </header>

  {#if pendingError}
    <div class="banner" role="alert">
      <span>{pendingError}</span>
      <button
        type="button"
        class="banner-dismiss"
        onclick={() => (pendingError = null)}
        aria-label="Dismiss error"
      >×</button>
    </div>
  {/if}

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

  .legend {
    margin-left: auto;
    font-size: 0.7rem;
    color: var(--fg-tertiary);
    letter-spacing: 0.04em;
  }

  .legend kbd {
    font-family: var(--font-mono, ui-monospace, SFMono-Regular, monospace);
    font-size: 0.7rem;
    padding: 0.05rem 0.3rem;
    border: 0.04rem solid var(--border);
    border-radius: 0.2rem;
    color: var(--fg-secondary, var(--fg));
    background: transparent;
  }

  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1rem;
    padding: 0.5rem 0.75rem;
    border: 0.04rem solid var(--danger);
    color: var(--danger);
    font-size: 0.8rem;
    border-radius: 0.2rem;
  }

  .banner-dismiss {
    background: transparent;
    border: none;
    color: inherit;
    font-size: 1rem;
    line-height: 1;
    cursor: pointer;
    padding: 0 0.25rem;
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
