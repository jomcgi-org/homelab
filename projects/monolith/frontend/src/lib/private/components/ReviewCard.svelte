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
    <span class="card-tab card-tab--{tab === 'gaps' ? 'gap' : 'note'}">
      {tab === "gaps" ? "gap" : "note"}
    </span>
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
      <button class="action action--keep" onclick={() => onDecide("yes")}>
        {#if tab === "gaps"}
          Keep (y)
        {:else}
          Public (y)
        {/if}
      </button>
      <button class="action action--reject" onclick={() => onDecide("no")}>
        {tab === "gaps" ? "Reject (n)" : "Private (n)"}
      </button>
    {:else}
      <button class="action action--agree" onclick={() => onDecide("yes")}
        >Agree (y)</button
      >
      <button class="action" onclick={() => onDecide("no")}>Flip (n)</button>
      <button class="action" onclick={() => onDecide("skip")}>Skip (s)</button>
      <button class="action action--danger" onclick={() => onDecide("delete")}
        >Delete (d)</button
      >
    {/if}
  </div>
</article>

<style>
  /* The card is the panel's body: a record partitioned by rules. The head
     row names the kind and the item, the metadata is a spec table, bodies
     are ruled sections, and the decisions close the record as a divided
     row of controls. */
  .card {
    display: flex;
    flex: 1 1 auto;
    flex-direction: column;
    min-height: 0;
    background: var(--sheet);
    color: var(--ink);
  }

  .card-header {
    display: grid;
    flex-shrink: 0;
    grid-template-columns: max-content 1fr max-content;
    align-items: center;
    gap: 1rem;
    padding: 0.9rem 1rem;
    border-bottom: 1px solid var(--stroke);
  }

  /* The kind marker keeps the graph's meaning colours (gap, note) as a bar. */
  .card-tab {
    padding: 0.25rem 0.5rem;
    border-left: 2px solid var(--tone-disk);
    color: var(--ink-2);
    font: 500 0.68rem var(--font-code);
    letter-spacing: 0.08em;
    text-transform: uppercase;
    white-space: nowrap;
  }

  .card-tab--note {
    border-left-color: var(--tone-ram);
  }

  .card-title {
    margin: 0;
    font-size: clamp(1.1rem, 2vw, 1.35rem);
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.2;
    word-break: break-word;
  }

  .card-badges {
    display: inline-flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    justify-content: flex-end;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.5rem;
    border: 1px solid var(--stroke);
    color: var(--ink-2);
    font: 0.68rem var(--font-code);
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }

  .badge-glyph {
    font-size: 0.8rem;
    line-height: 1;
  }

  .badge--ok {
    border-color: var(--ok);
    color: var(--ok);
  }

  .card-meta {
    display: grid;
    flex-shrink: 0;
    grid-template-columns: minmax(7em, 11em) minmax(0, 1fr);
    row-gap: 0;
    border-bottom: 1px solid var(--stroke);
  }

  .meta-key {
    padding: 0.5em 0.6em 0.5em 1rem;
    border-top: 1px solid var(--line);
    color: var(--ink-2);
    font: 500 0.72rem/1.5 var(--font-code);
  }

  .meta-val {
    padding: 0.5em 1rem 0.5em 1.2em;
    border-top: 1px solid var(--line);
    border-left: 1px solid var(--line);
    color: var(--ink);
    font-size: 0.85rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .meta-key:first-child,
  .meta-key:first-child + .meta-val {
    border-top: 0;
  }

  .meta-val--break {
    overflow-wrap: anywhere;
  }

  /* Tags are one divided value. */
  .card-tags {
    display: flex;
    flex-shrink: 0;
    flex-wrap: wrap;
    margin: -1px 0 0 -1px;
    border-bottom: 1px solid var(--stroke);
  }

  .tag {
    padding: 0.45em 0.9em;
    border-top: 1px solid var(--line);
    border-left: 1px solid var(--line);
    color: var(--ink-2);
    font: 0.72rem/1.5 var(--font-code);
  }

  /* Body sections: a mono label row over the text. The last one grows to
     fill the card and scrolls inside, so a long stub never pushes the
     decisions off the screen. */
  .card-body {
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    min-height: 0;
    border-bottom: 1px solid var(--stroke);
  }

  .card-body:last-of-type {
    flex: 1 1 auto;
  }

  .body-label {
    padding: 0.45em 1rem;
    border-bottom: 1px solid var(--line);
    background: var(--band);
    color: var(--ink-2);
    font: 500 0.68rem var(--font-code);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .body-scroll {
    position: relative;
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    scrollbar-width: thin;
  }

  .body-text {
    margin: 0;
    padding: 0.75rem 1rem;
    color: var(--ink);
    font: 0.82rem/1.6 var(--font-code);
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* Decisions: a row of divided cells, each its own control, the key hint
     in the label. The verdict colours mark the cell's left edge. */
  .actions {
    display: flex;
    flex-shrink: 0;
    flex-wrap: wrap;
    margin-top: auto;
  }

  .action {
    padding: 0.7em 1rem;
    border: 0;
    border-right: 1px solid var(--stroke);
    border-left: 2px solid transparent;
    background: var(--sheet);
    color: var(--ink);
    font: 0.78rem var(--font-code);
    cursor: pointer;
  }

  .action:hover {
    background: var(--band);
    color: var(--accent-ink);
  }

  .action:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }

  .action--keep,
  .action--agree {
    border-left-color: var(--ok);
  }

  .action--reject,
  .action--danger {
    border-left-color: var(--tone-disk);
  }
</style>
