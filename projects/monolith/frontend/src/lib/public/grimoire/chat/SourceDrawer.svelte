<script>
  // Right-side slide-over for a GROUNDED IN chip. Controlled by the parent:
  // `item` is the touched item to show ({id, title, kind, entity_type?,
  // book_id?, chunk_ref?}) or null when closed; `onclose` fires on every
  // dismissal path (X button, backdrop click, Escape) so the parent stays the
  // single source of truth for open/closed state.
  //
  // kind === "entity": fetches /entities/{id} and renders it with the SAME
  // statblock dispatcher the /entity/[id] page uses (EntityDetail ->
  // Creature/Spell/Generic), so a chip never re-implements stat-block layout.
  // kind === "chunk": fetches /chunks/{id} and renders the passage with the
  // reader's own renderChunk parser (structural blocks, never {@html}), then
  // links to the reader via chunkHref(book_id, id) -- the same resolver route
  // entity-mention links and search hits already use, which redirects into
  // the continuous reader positioned at that chunk.
  import { apiFetch, entityHref, chunkHref } from "$lib/public/grimoire/api.js";
  import { renderChunk } from "$lib/public/grimoire/renderChunk.js";
  import EntityDetail from "$lib/public/grimoire/statblock/EntityDetail.svelte";

  let { item = null, onclose = () => {} } = $props();

  const open = $derived(item != null);

  let entity = $state(null);
  let chunk = $state(null);
  let loading = $state(false);
  let error = $state("");

  let closeBtn = $state();
  let previouslyFocused = null;

  // Refetch whenever the identity of the open item changes: a click on a
  // different chip while the drawer is already open must not show stale
  // content from the previous one, guarded by an identity check on every
  // async resolution.
  $effect(() => {
    const current = item;
    entity = null;
    chunk = null;
    error = "";
    if (!current) return;
    loading = true;
    (async () => {
      try {
        if (current.kind === "entity") {
          const result = await apiFetch(
            `/entities/${encodeURIComponent(current.id)}`,
          );
          if (item === current) entity = result;
        } else {
          const result = await apiFetch(
            `/chunks/${encodeURIComponent(current.id)}`,
          );
          if (item === current) chunk = result;
        }
      } catch (e) {
        if (item === current) error = e.message || "Failed to load.";
      } finally {
        if (item === current) loading = false;
      }
    })();
  });

  // Focus management (basic, not a full trap): move focus to the close
  // button on open so keyboard/AT users land inside the drawer, and restore
  // focus to whatever opened it (the chip) on close.
  $effect(() => {
    if (open) {
      previouslyFocused =
        typeof document !== "undefined" ? document.activeElement : null;
      queueMicrotask(() => closeBtn?.focus());
    } else if (previouslyFocused) {
      previouslyFocused.focus?.();
      previouslyFocused = null;
    }
  });

  function close() {
    onclose();
  }

  function onWindowKeydown(e) {
    if (open && e.key === "Escape") {
      e.preventDefault();
      close();
    }
  }

  const chunkBlocks = $derived(chunk ? renderChunk(chunk.content) : []);
  const readerHref = $derived(
    chunk ? chunkHref(chunk.book_id, chunk.id) : null,
  );
  const entryHref = $derived(
    item?.kind === "entity" ? entityHref(item.id) : null,
  );
  const kindLabel = $derived(
    item?.kind === "entity" ? item.entity_type || "person or place" : "passage",
  );
  const headerLabel = $derived(
    item?.kind === "entity" && entity
      ? entity.name
      : item?.kind === "chunk" && chunk
        ? (chunk.section_path ?? item.title)
        : (item?.title ?? ""),
  );
</script>

<svelte:window onkeydown={onWindowKeydown} />

{#if open}
  <button
    type="button"
    class="src-backdrop"
    onclick={close}
    aria-label="Close source panel"
  ></button>

  <aside
    class="src-drawer"
    role="dialog"
    aria-modal="true"
    aria-label={headerLabel || "Source detail"}
  >
    <div class="src-head">
      <div class="src-head-text">
        <span class="src-kind">{kindLabel}</span>
        <h2 class="grim-title src-title">{headerLabel}</h2>
      </div>
      <button
        type="button"
        class="src-close"
        bind:this={closeBtn}
        onclick={close}
        aria-label="Close"
      >
        &times;
      </button>
    </div>

    <div class="src-body">
      {#if loading}
        <p class="src-status">Loading…</p>
      {:else if error}
        <p class="src-status src-status-error">{error}</p>
      {:else if item?.kind === "entity" && entity}
        <div class="src-statblock">
          <EntityDetail {entity} />
        </div>
        <a class="src-link" href={entryHref}>View full entry &rarr;</a>
      {:else if item?.kind === "chunk" && chunk}
        <div class="src-passage">
          {#each chunkBlocks as block, i (i)}
            {#if block.type === "heading"}
              <h3 class="src-passage-heading">{block.text}</h3>
            {:else if block.type === "list"}
              <ul class="src-passage-list">
                {#each block.items as li, lii (lii)}
                  <li>{li}</li>
                {/each}
              </ul>
            {:else}
              <p>{block.text}</p>
            {/if}
          {/each}
        </div>
        {#if readerHref}
          <a class="src-link" href={readerHref}>Read in the book &rarr;</a>
        {/if}
      {/if}
    </div>
  </aside>
{/if}

<style>
  .src-backdrop {
    position: fixed;
    inset: 0;
    z-index: 60;
    border: none;
    padding: 0;
    background: rgba(10, 14, 20, 0.4);
    cursor: default;
  }

  .src-drawer {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    z-index: 61;
    width: min(440px, 92vw);
    display: flex;
    flex-direction: column;
    background: var(--grim-surface);
    border-left: 1px solid var(--grim-line);
    box-shadow: -12px 0 32px rgba(10, 14, 20, 0.2);
    animation: src-slide-in 220ms ease-out;
  }

  @keyframes src-slide-in {
    from {
      transform: translateX(100%);
    }
    to {
      transform: translateX(0);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .src-drawer {
      animation: none;
    }
  }

  .src-head {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 20px 20px 16px;
    border-bottom: 1px solid var(--grim-line);
  }

  .src-head-text {
    flex: 1;
    min-width: 0;
  }

  .src-kind {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--grim-on-accent);
    background: var(--grim-accent);
    border-radius: 4px;
    padding: 2px 8px;
    margin-bottom: 8px;
  }

  .src-title {
    margin: 0;
    font-size: 20px;
    line-height: 1.25;
    color: var(--grim-ink);
    overflow-wrap: break-word;
  }

  .src-close {
    flex-shrink: 0;
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 20px;
    line-height: 1;
    border: 1px solid var(--grim-line);
    border-radius: 8px;
    background: var(--grim-surface);
    color: var(--grim-ink);
    cursor: pointer;
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }

  .src-close:hover {
    background: var(--grim-accent-soft);
    border-color: var(--grim-accent);
  }

  .src-body {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    padding: 20px;
    background: var(--grim-paper);
  }

  .src-statblock :global(.statblock),
  .src-statblock :global(.generic) {
    max-width: none;
  }

  .src-status {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--grim-text-faint);
  }

  .src-status-error {
    color: var(--grim-type-creature);
  }

  .src-passage {
    font-family: var(--grim-serif);
    font-size: 15px;
    line-height: 1.65;
    color: var(--grim-ink);
  }

  .src-passage p {
    margin: 0 0 12px;
  }

  .src-passage-heading {
    margin: 20px 0 8px;
    font-size: 16px;
    font-weight: 600;
  }

  .src-passage-heading:first-child {
    margin-top: 0;
  }

  .src-passage-list {
    margin: 0 0 12px 20px;
    padding: 0;
    list-style: disc;
  }

  .src-passage-list li {
    margin-bottom: 6px;
  }

  .src-link {
    display: inline-block;
    margin-top: 16px;
    font-family: var(--font-mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--grim-accent);
    text-decoration: none;
  }

  .src-link:hover {
    text-decoration: underline;
  }

  @media (max-width: 480px) {
    .src-drawer {
      width: 100vw;
    }
  }
</style>
