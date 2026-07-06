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
    <div class="card-hard skeleton-block"></div>
  {:else if notFound}
    <div class="empty">
      <p class="empty-lead display">Not found.</p>
      <p class="empty-help">This adventure isn't in the loaded corpus.</p>
    </div>
  {:else if error}
    <p class="mono status-error">{error}</p>
  {:else if adventure}
    <a class="eyebrow back-link" href={bookHref(adventure.book_id)}
      >&larr; {adventure.book_display_name.toUpperCase()}</a
    >
    <h1 class="display adventure-title">{adventure.name}</h1>
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
            <p class="mono roster-group-title">{group.entity_type}</p>
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
      <p class="mono empty-help roster-empty">
        No entities extracted in this range yet.
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

  .back-link {
    display: inline-block;
    align-self: flex-start;
  }

  .back-link:hover {
    color: var(--ink);
  }

  .adventure-title {
    font-size: clamp(28px, 5vw, 44px);
    margin-top: -8px;
  }

  .adventure-level {
    margin-top: -16px;
    color: var(--ink-3);
  }

  .adventure-summary {
    color: var(--ink-2);
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
    color: var(--ink-3);
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
    color: var(--ink);
  }

  .roster-empty {
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
    color: var(--ink-3);
  }

  .status-error {
    color: var(--coral);
    padding: 32px 0;
  }

  .skeleton-block {
    height: 320px;
    background: linear-gradient(
      90deg,
      var(--bg-elev) 25%,
      transparent 37%,
      var(--bg-elev) 63%
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
