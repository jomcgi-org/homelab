<script>
  // Props (Svelte 5 runes)
  let { item, tab, mode, onDecide } = $props();
  // item: object with fields depending on tab
  //   tab='gaps':  { id, term, context, gap_class, state?, resolved_at?,
  //                  human_verified, created_at? }
  //   tab='notes': { id, title, snippet, visibility?, visibility_verified,
  //                  updated_at? }
  // tab: 'gaps' | 'notes'
  // mode: 'pending' | 'audit'
  // onDecide: (action: 'yes' | 'no' | 'skip') => void
</script>

<article class="card">
  {#if tab === "gaps"}
    <h2 class="title">{item.term}</h2>
    <dl class="meta">
      <dt>Context</dt>
      <dd>{item.context}</dd>
      <dt>Class</dt>
      <dd>{item.gap_class}</dd>
      {#if mode === "audit"}
        {#if item.state}
          <dt>State</dt>
          <dd>{item.state}</dd>
        {/if}
        {#if item.resolved_at}
          <dt>Decided</dt>
          <dd>{item.resolved_at}</dd>
        {/if}
      {/if}
    </dl>
  {:else}
    <h2 class="title">{item.title}</h2>
    {#if item.snippet}
      <p class="snippet">{item.snippet}</p>
    {/if}
    {#if mode === "audit"}
      <dl class="meta">
        {#if item.visibility}
          <dt>Visibility</dt>
          <dd>{item.visibility}</dd>
        {/if}
        {#if item.updated_at}
          <dt>Updated</dt>
          <dd>{item.updated_at}</dd>
        {/if}
      </dl>
    {/if}
  {/if}

  <div class="actions">
    {#if mode === "pending"}
      <button class="action" onclick={() => onDecide("yes")}>
        {tab === "gaps" ? "Keep (y)" : "Public (y)"}
      </button>
      <button class="action" onclick={() => onDecide("no")}>
        {tab === "gaps" ? "Reject (n)" : "Private (n)"}
      </button>
    {:else}
      <button class="action" onclick={() => onDecide("yes")}>Agree (y)</button>
      <button class="action" onclick={() => onDecide("no")}>Re-open (n)</button>
      <button class="action" onclick={() => onDecide("skip")}>Skip (s)</button>
    {/if}
  </div>
</article>

<style>
  .card {
    border: 0.04rem solid var(--border);
    padding: 1rem 1.25rem;
    border-radius: 4px;
    background: var(--surface, transparent);
    font-family: var(--font);
    color: var(--fg);
  }

  .title {
    margin: 0 0 0.75rem 0;
    font-size: 1rem;
    font-weight: 700;
  }

  .snippet {
    margin: 0 0 0.75rem 0;
    font-size: 0.85rem;
    line-height: 1.5;
    color: var(--fg-secondary);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .meta {
    display: grid;
    grid-template-columns: max-content 1fr;
    gap: 0.25rem 1rem;
    margin: 0;
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .meta dt {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    padding-top: 0.15rem;
  }

  .meta dd {
    margin: 0;
    color: var(--fg-secondary);
    white-space: pre-wrap;
    word-break: break-word;
  }

  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 1.25rem;
  }

  .action {
    font-family: var(--font);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    background: transparent;
    border: 0.04rem solid var(--border);
    padding: 0.4rem 0.75rem;
    border-radius: 3px;
    cursor: pointer;
  }

  .action:hover {
    color: var(--fg);
    border-color: var(--fg);
  }

  .action:focus-visible {
    outline: 1.5px solid var(--fg);
    outline-offset: 2px;
  }
</style>
