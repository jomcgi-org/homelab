<script>
  // Prose-first renderer for entity types without a bespoke layout (location,
  // npc, faction, deity, item). Every non-meta field renders as a labeled
  // inline value, a labeled paragraph, or labeled normalized blocks -- JSONB
  // overflow NEVER falls back to raw JSON.
  import {
    META_FIELDS,
    formatFieldName,
    isProse,
    normalizeBlocks,
    scalarToText,
  } from "$lib/public/grimoire/format.js";

  let { data } = $props();

  const fields = $derived(
    Object.entries(data ?? {})
      .filter(([k, v]) => !META_FIELDS.has(k) && v != null && v !== "")
      .map(([key, value]) => {
        if (typeof value === "object") {
          return { key, kind: "blocks", blocks: normalizeBlocks(value) };
        }
        if (isProse(key, value)) {
          return { key, kind: "prose", text: String(value) };
        }
        return { key, kind: "inline", text: scalarToText(value) };
      })
      .filter((f) => f.kind !== "blocks" || f.blocks.length),
  );
</script>

<article class="card-hard frame">
  <h2 class="display name">{data.name}</h2>
  {#if data.entity_type}
    <p class="eyebrow strap">{data.entity_type}</p>
  {/if}

  <hr class="rule" />

  {#if fields.length === 0}
    <p class="none">No further details recorded.</p>
  {:else}
    <div class="fields">
      {#each fields as field (field.key)}
        {#if field.kind === "inline"}
          <div class="row">
            <span class="eyebrow key">{formatFieldName(field.key)}</span>
            <span class="val">{field.text}</span>
          </div>
        {:else if field.kind === "prose"}
          <div class="prose">
            <span class="eyebrow key">{formatFieldName(field.key)}</span>
            <p class="para">{field.text}</p>
          </div>
        {:else}
          <div class="prose">
            <span class="eyebrow key">{formatFieldName(field.key)}</span>
            {#each field.blocks as b, i (i)}
              <p class="para">
                {#if b.name}<strong>{b.name}.</strong>{/if}
                {b.text}
              </p>
            {/each}
          </div>
        {/if}
      {/each}
    </div>
  {/if}
</article>

<style>
  .frame {
    padding: clamp(20px, 4vw, 32px);
    border-top: 4px solid var(--coral);
    max-width: 640px;
  }

  .name {
    font-size: clamp(28px, 5vw, 40px);
  }

  .strap {
    margin-top: 4px;
  }

  .rule {
    border: none;
    border-top: 2px solid var(--rule);
    margin: 16px 0;
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 12px;
    align-items: baseline;
  }

  .key {
    min-width: 9em;
    flex-shrink: 0;
  }

  .val {
    font-weight: 700;
  }

  .prose .key {
    display: block;
    margin-bottom: 6px;
  }

  .para {
    line-height: 1.65;
    margin-bottom: 6px;
  }

  .none {
    font-style: italic;
    color: var(--ink-3);
  }
</style>
