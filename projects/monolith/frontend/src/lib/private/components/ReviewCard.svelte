<script>
  // Props (Svelte 5 runes)
  let { item, tab, mode, onDecide } = $props();
  // item: object with fields depending on tab
  //   tab='gaps':  { id, term, context, gap_class, state?, resolved_at?,
  //                  human_verified, created_at?, referenced_by_count?,
  //                  research_attempts?, answer?, stub_body?, deleted_at? }
  //   tab='notes': { id, title, snippet, visibility?, visibility_verified,
  //                  updated_at?, tags?, type?, source?, deleted_at? }
  // tab: 'gaps' | 'notes'
  // mode: 'pending' | 'audit'
  // onDecide: (action: 'yes' | 'no' | 'skip' | 'delete') => void

  // Format ISO timestamps as compact "Sat 23 May 19:28" — matches the
  // density elsewhere on /private without spelling out the year or
  // seconds, which would dominate the metadata grid.
  function fmtTimestamp(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return iso;
      return d.toLocaleString("en-GB", {
        weekday: "short",
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      });
    } catch {
      return iso;
    }
  }

  // Gap badges: only emit when the underlying number is meaningful.
  // referenced_by_count shows even at 0 because "no notes link here" is a
  // real signal during audit; research_attempts is silent at 0 (the gap
  // hasn't been auto-researched yet, which isn't worth a badge).
  let refsLabel = $derived(
    tab === "gaps" && typeof item.referenced_by_count === "number"
      ? `${item.referenced_by_count} ref${item.referenced_by_count === 1 ? "" : "s"}`
      : null,
  );
  let attemptsLabel = $derived(
    tab === "gaps" && (item.research_attempts ?? 0) > 0
      ? `${item.research_attempts} attempt${item.research_attempts === 1 ? "" : "s"}`
      : null,
  );
</script>

<article class="card">
  <header class="card-header">
    <span class="card-tab">{tab === "gaps" ? "gap" : "note"}</span>
    <h2 class="card-title">
      {tab === "gaps" ? item.term : item.title}
    </h2>
    <div class="card-badges">
      {#if refsLabel}
        <span class="badge" title="Notes that wikilink at this term">
          <span class="badge-glyph">&#8599;</span>{refsLabel}
        </span>
      {/if}
      {#if attemptsLabel}
        <span class="badge" title="Auto-research attempts">
          <span class="badge-glyph">&#8635;</span>{attemptsLabel}
        </span>
      {/if}
      {#if tab === "notes" && item.visibility_verified}
        <span class="badge badge--ok">verified</span>
      {/if}
    </div>
  </header>

  <div class="card-meta">
    {#if tab === "gaps"}
      {#if item.context}
        <span class="meta-key">Context</span>
        <span class="meta-val">{item.context}</span>
      {/if}
      <span class="meta-key">Class</span>
      <span class="meta-val">{item.gap_class}</span>
      {#if mode === "audit"}
        {#if item.state}
          <span class="meta-key">State</span>
          <span class="meta-val">{item.state}</span>
        {/if}
        {#if item.resolved_at}
          <span class="meta-key">Decided</span>
          <span class="meta-val">{fmtTimestamp(item.resolved_at)}</span>
        {/if}
      {/if}
    {:else}
      {#if item.type}
        <span class="meta-key">Type</span>
        <span class="meta-val">{item.type}</span>
      {/if}
      {#if item.source}
        <span class="meta-key">Source</span>
        <span class="meta-val meta-val--break">{item.source}</span>
      {/if}
      {#if mode === "audit"}
        {#if item.visibility}
          <span class="meta-key">Visibility</span>
          <span class="meta-val">{item.visibility}</span>
        {/if}
        {#if item.updated_at}
          <span class="meta-key">Updated</span>
          <span class="meta-val">{fmtTimestamp(item.updated_at)}</span>
        {/if}
      {/if}
    {/if}
  </div>

  {#if tab === "notes" && item.tags?.length}
    <div class="card-tags" aria-label="Tags">
      {#each item.tags as t}
        <span class="tag">{t}</span>
      {/each}
    </div>
  {/if}

  <!-- Body subpanel: gaps render answer (when present, mostly in audit
       mode on committed gaps) + the stub body underneath. Notes render
       the snippet. Both are clipped to a max-height with a fade-out so a
       long body doesn't dominate the viewport. -->
  {#if tab === "gaps" && item.answer}
    <section class="card-body" aria-label="Captured answer">
      <span class="body-label">Answer</span>
      <pre class="body-text">{item.answer}</pre>
    </section>
  {/if}
  {#if tab === "gaps" && item.stub_body}
    <section class="card-body" aria-label="Stub body">
      <span class="body-label">Stub</span>
      <div class="body-scroll">
        <pre class="body-text">{item.stub_body}</pre>
      </div>
    </section>
  {/if}
  {#if tab === "notes" && item.snippet}
    <section class="card-body" aria-label="Note snippet">
      <span class="body-label">Snippet</span>
      <div class="body-scroll">
        <pre class="body-text">{item.snippet}</pre>
      </div>
    </section>
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
      <button class="action" onclick={() => onDecide("no")}>Flip (n)</button>
      <button class="action" onclick={() => onDecide("skip")}>Skip (s)</button>
      <button
        class="action action--danger"
        onclick={() => onDecide("delete")}
      >Delete (d)</button>
    {/if}
  </div>
</article>

<style>
  .card {
    border: 0.04rem solid var(--border);
    background: var(--surface);
    padding: 1.25rem 1.5rem;
    border-radius: 4px;
    font-family: var(--font);
    color: var(--fg);
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  /* ── Header strip ─────────────────────────────────────────────── */

  .card-header {
    display: grid;
    grid-template-columns: max-content 1fr max-content;
    align-items: baseline;
    gap: 0.75rem;
    padding-bottom: 0.6rem;
    border-bottom: 0.04rem solid var(--border);
  }

  .card-tab {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    padding: 0.15rem 0.4rem;
    border: 0.04rem solid var(--fg-tertiary);
    border-radius: 2px;
  }

  .card-title {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 700;
    line-height: 1.3;
    word-break: break-word;
  }

  .card-badges {
    display: inline-flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .badge {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--fg-secondary);
    padding: 0.15rem 0.4rem;
    border: 0.04rem solid var(--border);
    border-radius: 2px;
    background: var(--bg);
    font-variant-numeric: tabular-nums;
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    white-space: nowrap;
  }

  .badge-glyph {
    font-size: 0.75rem;
    line-height: 1;
    color: var(--fg-tertiary);
  }

  .badge--ok {
    color: var(--st-ok, var(--fg));
    border-color: var(--st-ok, var(--border));
  }

  /* ── Metadata grid ────────────────────────────────────────────── */

  .card-meta {
    display: grid;
    grid-template-columns: max-content 1fr;
    column-gap: 1rem;
    row-gap: 0.25rem;
    font-size: 0.8rem;
    line-height: 1.5;
  }

  .meta-key {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
    padding-top: 0.15rem;
  }

  .meta-val {
    color: var(--fg-secondary);
    word-break: break-word;
    white-space: pre-wrap;
  }

  .meta-val--break {
    /* Source URLs / file paths can be long-don't push the grid wider. */
    overflow-wrap: anywhere;
  }

  /* ── Tags ─────────────────────────────────────────────────────── */

  .card-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
  }

  .tag {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--fg-secondary);
    padding: 0.15rem 0.4rem;
    border: 0.04rem solid var(--border);
    border-radius: 2px;
    background: var(--bg);
  }

  /* ── Body subpanel (snippet / stub / answer) ──────────────────── */

  .card-body {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    border: 0.04rem solid var(--border);
    border-radius: 2px;
    background: var(--bg);
    padding: 0.6rem 0.75rem;
  }

  .body-label {
    font-size: 0.65rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg-tertiary);
  }

  .body-scroll {
    position: relative;
    max-height: 24rem;
    overflow: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--fg-tertiary) transparent;
  }

  .body-scroll::-webkit-scrollbar {
    width: 4px;
  }

  .body-scroll::-webkit-scrollbar-thumb {
    background: var(--fg-tertiary);
    border-radius: 2px;
  }

  .body-text {
    margin: 0;
    font-family: var(--font-mono);
    font-size: 0.78rem;
    line-height: 1.55;
    color: var(--fg-secondary);
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* ── Actions ──────────────────────────────────────────────────── */

  .actions {
    display: flex;
    gap: 0.5rem;
    margin-top: 0.25rem;
    justify-content: flex-end;
    flex-wrap: wrap;
  }

  .action {
    font-family: var(--font);
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    background: var(--bg);
    border: 0.04rem solid var(--border);
    padding: 0.4rem 0.75rem;
    border-radius: 2px;
    cursor: pointer;
  }

  .action:hover {
    background: var(--surface);
  }

  .action:focus-visible {
    outline: 1.5px solid var(--fg);
    outline-offset: 2px;
  }

  .action--danger {
    color: var(--danger);
    border-color: var(--danger);
  }

  .action--danger:hover {
    background: var(--danger);
    color: var(--bg);
  }
</style>
