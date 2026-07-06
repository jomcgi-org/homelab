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

<article class="generic grim-paper">
  <h2 class="grim-title name">{data.name}</h2>
  {#if data.entity_type}
    <p class="grim-smallcaps strap">{data.entity_type}</p>
  {/if}

  <hr class="grim-rule" />

  {#if fields.length === 0}
    <p class="none">No further details recorded.</p>
  {:else}
    <div class="fields">
      {#each fields as field (field.key)}
        {#if field.kind === "inline"}
          <div class="row">
            <span class="key">{formatFieldName(field.key)}</span>
            <span class="val">{field.text}</span>
          </div>
        {:else if field.kind === "prose"}
          <div class="prose">
            <span class="key">{formatFieldName(field.key)}</span>
            <p class="para">{field.text}</p>
          </div>
        {:else}
          <div class="prose">
            <span class="key">{formatFieldName(field.key)}</span>
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
  .generic {
    padding: clamp(1rem, 3vw, 1.5rem);
    border: 1px solid var(--grim-paper-line);
    border-top: 3px solid var(--grim-accent);
    max-width: 40rem;
  }

  .name {
    font-size: clamp(1.4rem, 4vw, 1.9rem);
    color: var(--grim-accent-strong);
  }

  .strap {
    font-size: 0.8rem;
    color: var(--grim-ink-soft);
  }

  .fields {
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
  }

  .row {
    display: flex;
    gap: 0.75rem;
    align-items: baseline;
  }

  .key {
    font-family: var(--font-mono);
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--grim-ink-soft);
    min-width: 7rem;
    flex-shrink: 0;
  }

  .val {
    font-family: var(--grim-serif);
    color: var(--grim-ink);
  }

  .prose .key {
    display: block;
    margin-bottom: 0.2rem;
  }

  .para {
    font-family: var(--grim-serif);
    font-size: 0.95rem;
    line-height: 1.55;
    color: var(--grim-ink);
    margin-bottom: 0.3rem;
  }

  .none {
    font-family: var(--grim-serif);
    font-style: italic;
    color: var(--grim-ink-soft);
  }
</style>
