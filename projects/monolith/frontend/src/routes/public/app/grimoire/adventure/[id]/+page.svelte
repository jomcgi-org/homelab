<script>
  // Public adventure detail: name, level range, summary, link back to the
  // parent book, and the entity roster grouped by entity_type. The API
  // already orders entities by entity_type then name (grimoire/library.py
  // adventure_entities), so grouping here is a plain scan, no client sort.
  import { page } from "$app/stores";
  import { apiFetch, bookHref, entityHref } from "$lib/public/grimoire/api.js";

  const adventureId = $derived($page.params.id);

  let adventure = $state(null);
  let loading = $state(true);
  let error = $state("");
  let notFound = $state(false);

  $effect(() => {
    load(adventureId);
  });

  async function load(id) {
    loading = true;
    error = "";
    notFound = false;
    adventure = null;
    try {
      adventure = await apiFetch(`/adventures/${encodeURIComponent(id)}`);
    } catch (e) {
      console.error("Could not load adventure", e);
      const msg = String(e.message);
      if (msg.includes("404") || msg.toLowerCase().includes("not found")) {
        notFound = true;
      } else {
        error = e.message;
      }
    } finally {
      loading = false;
    }
  }

  const groups = $derived.by(() => {
    if (!adventure) return [];
    const out = [];
    for (const ent of adventure.entities) {
      const last = out[out.length - 1];
      if (last && last.entity_type === ent.entity_type) {
        last.items.push(ent);
      } else {
        out.push({ entity_type: ent.entity_type, items: [ent] });
      }
    }
    return out;
  });
</script>

<div class="wrap-narrow detail-page page">
  {#if loading}
    <div class="skeleton-block"></div>
  {:else if notFound}
    <div class="empty">
      <p class="grim-title empty-lead">Not found.</p>
      <p class="empty-help">Nothing by that name in the books loaded here.</p>
    </div>
  {:else if error}
    <p class="status-error">
      Could not load this right now. Try again in a moment.
    </p>
  {:else if adventure}
    <a class="eyebrow back-link" href={bookHref(adventure.book_id)}
      >&larr; {adventure.book_display_name.toUpperCase()}</a
    >
    <h1 class="grim-title adventure-title">{adventure.name}</h1>
    {#if adventure.level_range}
      <p class="eyebrow adventure-level">LEVEL {adventure.level_range}</p>
    {/if}
    {#if adventure.summary}
      <p class="adventure-summary">{adventure.summary}</p>
    {/if}

    {#if groups.length}
      <section class="side-section">
        <h3 class="eyebrow head">Roster</h3>
        {#each groups as group (group.entity_type)}
          <div class="roster-group">
            <p class="roster-group-title">{group.entity_type}</p>
            <ul class="rel-list">
              {#each group.items as ent (ent.id)}
                <li class="rel">
                  <a class="rel-name" href={entityHref(ent.id)}>{ent.name}</a>
                </li>
              {/each}
            </ul>
          </div>
        {/each}
      </section>
    {:else}
      <p class="empty-help roster-empty">
        No characters or places recorded for this adventure yet.
      </p>
    {/if}
  {/if}
</div>

<style>
  .detail-page {
    padding: 48px 32px 96px;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .back-link {
    display: inline-block;
    align-self: flex-start;
  }

  .back-link:hover {
    color: var(--grim-ink);
  }

  .adventure-title {
    font-size: clamp(28px, 5vw, 44px);
    margin-top: -8px;
  }

  .adventure-level {
    margin-top: -16px;
  }

  .adventure-summary {
    color: var(--grim-text-dim);
    font-size: 16px;
    line-height: 1.6;
  }

  .head {
    margin-bottom: 12px;
  }

  .roster-group {
    margin-bottom: 20px;
  }

  .roster-group:last-child {
    margin-bottom: 0;
  }

  .roster-group-title {
    font-family: var(--font-mono);
    color: var(--grim-text-faint);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    font-size: 11px;
    margin-bottom: 8px;
  }

  .rel-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .rel {
    display: flex;
    align-items: center;
    min-height: 32px;
  }

  .rel-name {
    text-decoration: underline;
    text-underline-offset: 2px;
  }

  .rel-name:hover {
    color: var(--grim-ink);
  }

  .roster-empty {
    font-family: var(--font-mono);
    font-size: 12px;
  }

  .empty {
    padding: 64px 0;
    text-align: center;
  }

  .empty-lead {
    font-size: 28px;
    margin-bottom: 8px;
  }

  .empty-help {
    color: var(--grim-text-faint);
  }

  .status-error {
    font-family: var(--font-mono);
    color: var(--grim-type-creature);
    padding: 32px 0;
  }

  .skeleton-block {
    height: 320px;
    border-radius: 7px;
    background: linear-gradient(
      90deg,
      var(--grim-surface-2) 25%,
      transparent 37%,
      var(--grim-surface-2) 63%
    );
    background-size: 400% 100%;
    animation: shimmer 1.4s ease infinite;
  }

  @keyframes shimmer {
    0% {
      background-position: 100% 0;
    }
    100% {
      background-position: 0 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .skeleton-block {
      animation: none;
    }
  }
</style>
