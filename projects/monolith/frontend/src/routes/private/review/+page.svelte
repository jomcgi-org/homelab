<script>
  import { page } from "$app/stores";
  import { goto, invalidateAll } from "$app/navigation";
  import { deserialize } from "$app/forms";
  import ReviewCard from "$lib/private/components/ReviewCard.svelte";
  import ModeToggle from "$lib/private/components/ModeToggle.svelte";
  import UndoToast from "$lib/private/components/UndoToast.svelte";

  let { data } = $props();

  // Cursor for j/k navigation + skip. y/n/delete do NOT touch this --
  // they add the item's id to `processedIds` so the visible list
  // shrinks and the next item slides into place automatically.
  let index = $state(0);
  let pendingError = $state(null);

  // Items the user has acted on (y/n/delete) in this session. We filter
  // these out of data.items so a slow backend round-trip can't make a
  // verified item reappear after invalidateAll(). When tab/mode changes
  // or invalidateAll completes, this set is reconciled against the new
  // data.items so stale ids drop out.
  let processedIds = $state(new Set());

  // Active undo toast — null when no recent deletion. Shape:
  //   { tab, id, label, key } where key is a monotonic counter so the
  //   <UndoToast> re-mounts (and re-arms its timer) when a new delete
  //   happens before the previous toast expires.
  let tombstone = $state(null);
  let toastCounter = 0;

  // Items the user has NOT yet acted on. The user sees this filtered
  // list; index points into it. When y/n/delete fires, the acted item
  // moves to processedIds, this list shrinks by 1, and current
  // advances automatically without an explicit index++.
  let visibleItems = $derived(
    data.items.filter((it) => !processedIds.has(String(it.id))),
  );
  let current = $derived(visibleItems[index]);

  // Keep index in range when the visible list shrinks (action removes
  // an item, invalidate returns fewer items, etc.).
  $effect(() => {
    if (visibleItems.length === 0) {
      if (index !== 0) index = 0;
      return;
    }
    if (index > visibleItems.length - 1) {
      index = Math.max(0, visibleItems.length - 1);
    }
  });

  function resetSession() {
    index = 0;
    pendingError = null;
    processedIds = new Set();
  }

  function setTab(t) {
    const url = new URL($page.url);
    url.searchParams.set("tab", t);
    resetSession();
    goto(url, { replaceState: true, invalidateAll: true });
  }

  function setMode(m) {
    const url = new URL($page.url);
    url.searchParams.set("mode", m);
    resetSession();
    goto(url, { replaceState: true, invalidateAll: true });
  }

  function endpointFor(tab, mode, action, item) {
    if (tab === "gaps") {
      if (mode === "pending") {
        // Gaps reaching the pending queue are internal/hybrid awaiting an
        // answer (external gaps stay 'discovered' for the research routine
        // and never surface here). "yes" is an acknowledgement -> /verify;
        // "no" rejects.
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

  function deletePathFor(tab, item) {
    return tab === "gaps"
      ? `/api/knowledge/gaps/${item.id}`
      : `/api/knowledge/notes/${item.id}`;
  }

  function undeletePathFor(tab, id) {
    return tab === "gaps"
      ? `/api/knowledge/gaps/${id}/undelete`
      : `/api/knowledge/notes/${id}/undelete`;
  }

  function tombstoneLabel(tab, item) {
    const noun = tab === "gaps" ? "gap" : "note";
    const name = tab === "gaps" ? item.term : item.title;
    if (!name) return `Deleted ${noun}`;
    // Trim long titles so the toast stays one line.
    const trimmed = name.length > 64 ? name.slice(0, 61) + "..." : name;
    return `Deleted ${noun} "${trimmed}"`;
  }

  // j/k preview navigation -- doesn't process anything. Bounded by
  // visibleItems length so we never land on an undefined card.
  function advance() {
    if (index < visibleItems.length - 1) index++;
  }

  function back() {
    if (index > 0) index--;
  }

  // Add an item id to processedIds (= "this is no longer visible to me
  // in this session"). Returns a rollback closure that removes it again
  // on failure.
  function markProcessed(id) {
    const key = String(id);
    const next = new Set(processedIds);
    next.add(key);
    processedIds = next;
    return () => {
      const rollback = new Set(processedIds);
      rollback.delete(key);
      processedIds = rollback;
    };
  }

  // Apart from `delete`, every action POSTs to a /verify, /reject,
  // /reopen, or visibility endpoint via one of two form-action variants
  // (with or without a JSON body). Delete uses the DELETE-method
  // proxy action. All four share the same optimistic-advance pattern:
  // bump the index, fire, roll back on failure (except 404 which is
  // silently swallowed — the item went away in another tab).
  async function handleDecide(action) {
    if (!current) return;
    if (action === "skip") {
      advance();
      return;
    }

    if (action === "delete") {
      // Guard: `d` is wired in audit mode only; the button is also only
      // rendered in audit mode, but double-check so a future caller
      // can't trigger a hard-delete in pending by accident.
      if (data.mode !== "audit") return;
      await handleDelete();
      return;
    }

    const target = current;
    const { path, body } = endpointFor(data.tab, data.mode, action, target);

    // Any further decision clears the previous undo toast — the user has
    // moved on, and stale tombstones are confusing.
    tombstone = null;

    // Optimistic: hide the item from the visible queue immediately.
    // Next item slides into `current` via the derived store. Rollback
    // restores the item if the backend rejects.
    const rollback = markProcessed(target.id);

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
        // 404 from a decide path means the row was already gone in
        // another tab; keep it processed-and-hidden, no error UI.
        if (result.status === 404) {
          pendingError = null;
        } else {
          rollback();
          pendingError = result.data?.error ?? "Decide failed";
          return;
        }
      } else {
        pendingError = null;
      }
      // Refill the visible queue when we're near the end. processedIds
      // persists across the refetch so any items the backend hasn't
      // committed yet stay hidden until the next setTab/setMode reset.
      if (visibleItems.length <= 3) {
        await invalidateAll();
      }
    } catch (e) {
      rollback();
      pendingError = e?.message ?? String(e);
    }
  }

  async function handleDelete() {
    const target = current;
    if (!target) return;
    const path = deletePathFor(data.tab, target);
    const label = tombstoneLabel(data.tab, target);

    // Optimistic: hide the card + arm the undo toast immediately.
    const rollback = markProcessed(target.id);
    toastCounter += 1;
    tombstone = {
      tab: data.tab,
      id: target.id,
      label,
      key: toastCounter,
    };

    const formData = new FormData();
    formData.set("path", path);

    try {
      const res = await fetch(`?/deleteAction`, {
        method: "POST",
        body: formData,
      });
      const result = deserialize(await res.text());
      if (result.type === "failure" || result.type === "error") {
        // 404: already gone — leave the toast up so the user can still
        // hit Undo if they meant to keep it (undelete is idempotent on
        // the backend in the unknown-id case it'll just 404 silently).
        if (result.status !== 404) {
          rollback();
          tombstone = null;
          pendingError = result.data?.error ?? "Delete failed";
          return;
        }
      }
      pendingError = null;
      if (visibleItems.length <= 3) {
        await invalidateAll();
      }
    } catch (e) {
      rollback();
      tombstone = null;
      pendingError = e?.message ?? String(e);
    }
  }

  async function handleUndo() {
    if (!tombstone) return;
    const t = tombstone;
    // Optimistically dismiss the toast and unhide the item from the
    // visible queue so a successful undo immediately brings it back.
    tombstone = null;
    const restored = new Set(processedIds);
    restored.delete(String(t.id));
    processedIds = restored;

    const formData = new FormData();
    formData.set("path", undeletePathFor(t.tab, t.id));

    try {
      const res = await fetch(`?/undeleteAction`, {
        method: "POST",
        body: formData,
      });
      const result = deserialize(await res.text());
      if (result.type === "failure" || result.type === "error") {
        // 404: gap/note doesn't exist (impossible if we just deleted it)
        // or note isn't in deleted state — either way nothing to undo.
        // 409: gap was not deleted (idempotency edge). Treat both as
        // best-effort and surface a quiet inline error.
        pendingError = result.data?.error ?? "Undo failed";
        // Re-hide the item; the visible queue should match server truth.
        markProcessed(t.id);
        return;
      }
      pendingError = null;
      // Refresh the queue so the restored item shows back up.
      await invalidateAll();
    } catch (e) {
      pendingError = e?.message ?? String(e);
      markProcessed(t.id);
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
        case "d":
          if (data.mode === "audit") {
            e.preventDefault();
            handleDecide("delete");
          }
          break;
        case "u":
          if (tombstone) {
            e.preventDefault();
            handleUndo();
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

    {#if visibleItems.length}
      <span class="counter" aria-label="Queue position"
        >{index + 1} / {visibleItems.length}</span
      >
    {/if}
  </header>

  {#if pendingError}
    <div class="banner" role="alert">
      <span>{pendingError}</span>
      <button
        type="button"
        class="banner-dismiss"
        onclick={() => (pendingError = null)}
        aria-label="Dismiss error">×</button
      >
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
  {/if}
</section>

{#if tombstone}
  {#key tombstone.key}
    <UndoToast
      label={tombstone.label}
      onUndo={handleUndo}
      onDismiss={() => (tombstone = null)}
    />
  {/key}
{/if}

<style>
  /* Page chrome: brutalist counterpart to Nav. 2px black border on the
     bar (matching --border-heavy), explicit monospace throughout, and
     the active-tab underline mirrors Nav.svelte's coral-on-hover /
     black-on-active so this page reads as a sibling of the site nav. */

  /* Single-screen layout: the whole review surface fits in one viewport
     so the user can power through items without scrolling. The Card
     flex-grows to fill remaining height; its body subpanels scroll
     internally when content overflows. */
  .review {
    padding: 1.5rem 2.5rem 1.5rem;
    font-family: var(--font-mono);
    color: var(--fg);
    background: var(--bg);
    height: calc(100vh - 4rem); /* 4rem reserves the site Nav header */
    display: flex;
    flex-direction: column;
    gap: 1rem;
    overflow: hidden;
  }

  .bar {
    display: flex;
    gap: 1.5rem;
    align-items: center;
    padding-bottom: 0.75rem;
    border-bottom: var(--border-heavy);
    flex-shrink: 0;
  }

  /* Flat-by-default, pop-on-hover. Active tab is a filled yellow block
     that sits flat (already chosen, no need to advertise interactivity);
     inactive tabs gain a border + offset shadow on hover. */
  .tabs {
    display: inline-flex;
    gap: 0.6rem;
    align-items: center;
  }

  .tabs button {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    background: transparent;
    border: 2px solid transparent;
    padding: 0.45rem 0.75rem;
    cursor: pointer;
    transform: translate(0, 0);
    transition:
      transform 0.08s ease,
      box-shadow 0.08s ease,
      background 0.08s ease,
      color 0.08s ease,
      border-color 0.08s ease;
  }

  .tabs button:hover:not(.active) {
    color: var(--fg);
    border-color: var(--fg);
    transform: translate(-2px, -2px);
    box-shadow: 4px 4px 0 0 var(--fg);
  }

  .tabs button.active {
    color: var(--fg);
    background: var(--yellow);
    border-color: var(--fg);
  }

  .counter {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 700;
    color: var(--fg);
    letter-spacing: 0.08em;
    font-variant-numeric: tabular-nums;
  }

  .banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 1.25rem;
    padding: 0.65rem 0.85rem;
    border: 2px solid var(--fg);
    background: var(--coral);
    color: var(--fg);
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 700;
  }

  .banner-dismiss {
    background: transparent;
    border: none;
    color: inherit;
    font-size: 1.1rem;
    line-height: 1;
    cursor: pointer;
    padding: 0 0.25rem;
    font-weight: 700;
  }

  .error {
    color: var(--fg);
    background: var(--coral);
    border: 2px solid var(--fg);
    padding: 0.65rem 0.85rem;
    font-family: var(--font-mono);
    font-size: 0.9rem;
    font-weight: 700;
  }

  .empty {
    color: var(--fg-tertiary);
    font-family: var(--font-mono);
    font-size: 0.9rem;
    letter-spacing: 0.04em;
  }
</style>
