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
        {tab === "gaps" ? "Keep (y)" : "Public (y)"}
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
  /* Neo-brutalist treatment: 2px solid black borders, pure white surface,
     hard 0px corners, explicit monospace everywhere (so h2 does not fall
     back to the browser serif default), bright accent fills for the type
     badge and danger action, and inverted hover on buttons. Matches the
     hard-edge / mono / uppercase language of Nav.svelte and the
     --border-heavy token. */

  .card {
    border: var(--border-heavy);
    background: var(--bg);
    padding: 1.25rem 1.5rem;
    font-family: var(--font-mono);
    color: var(--fg);
    display: flex;
    flex-direction: column;
    gap: 1rem;
    /* Single-screen layout: fill the parent's remaining height so the
       review surface fits in one viewport. Body subpanels scroll
       internally; actions stay pinned at the bottom. */
    flex: 1 1 auto;
    min-height: 0;
  }

  /* ── Header strip ─────────────────────────────────────────────── */

  .card-header {
    display: grid;
    grid-template-columns: max-content 1fr max-content;
    align-items: center;
    gap: 1rem;
    padding-bottom: 1rem;
    border-bottom: var(--border-heavy);
    flex-shrink: 0;
  }

  .card-tab {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    padding: 0.3rem 0.6rem;
    border: 2px solid var(--fg);
    background: var(--bg);
    white-space: nowrap;
  }

  /* Aligns with the knowledge graph cluster palette:
       --cluster-gap   = coral
       --cluster-paper = green (used for notes here)
     so a gap card visually rhymes with a gap node in the graph. */
  .card-tab--gap {
    background: var(--coral);
  }

  .card-tab--note {
    background: var(--green);
  }

  .card-title {
    margin: 0;
    /* Explicit font-family so the h2 user-agent serif default does NOT
       win over the inherited card-level mono. */
    font-family: var(--font-mono);
    font-size: 1.5rem;
    font-weight: 800;
    line-height: 1.2;
    letter-spacing: -0.02em;
    color: var(--fg);
    word-break: break-word;
  }

  .card-badges {
    display: inline-flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .badge {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg);
    padding: 0.3rem 0.55rem;
    border: 2px solid var(--fg);
    background: var(--bg);
    font-variant-numeric: tabular-nums;
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    white-space: nowrap;
  }

  .badge-glyph {
    font-size: 0.8rem;
    line-height: 1;
    color: var(--fg);
  }

  .badge--ok {
    background: var(--st-ok);
    color: var(--bg);
    border-color: var(--st-ok);
  }

  /* ── Metadata grid ────────────────────────────────────────────── */

  .card-meta {
    display: grid;
    grid-template-columns: max-content 1fr;
    column-gap: 1.5rem;
    row-gap: 0.5rem;
    font-family: var(--font-mono);
    font-size: 0.9rem;
    line-height: 1.5;
  }

  .meta-key {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    padding-top: 0.2rem;
  }

  .meta-val {
    color: var(--fg);
    font-family: var(--font-mono);
    word-break: break-word;
    white-space: pre-wrap;
  }

  .meta-val--break {
    overflow-wrap: anywhere;
  }

  /* ── Tags ─────────────────────────────────────────────────────── */

  .card-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .tag {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.12em;
    color: var(--fg);
    padding: 0.25rem 0.55rem;
    border: 2px solid var(--fg);
    background: var(--cream);
  }

  /* ── Body subpanel (snippet / stub / answer) ──────────────────── */

  /* The LAST card-body (which is always the big one -- snippet or stub)
     flex-grows so the card fills available vertical space. Earlier
     bodies (a captured answer above the stub for gaps) stay
     natural-sized and DO NOT shrink -- otherwise their inner <pre>
     overflows the box and visually bleeds into the next subpanel. */
  .card-body {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    border: var(--border-heavy);
    background: var(--bg);
    padding: 0.85rem 1rem;
    flex-shrink: 0;
  }

  .card-body:last-of-type {
    flex: 1 1 auto;
    min-height: 0;
  }

  .body-label {
    font-family: var(--font-mono);
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    background: var(--cream);
    padding: 0.25rem 0.55rem;
    border: 2px solid var(--fg);
    align-self: flex-start;
  }

  .body-scroll {
    position: relative;
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
    scrollbar-width: thin;
    scrollbar-color: var(--fg) transparent;
  }

  .body-scroll::-webkit-scrollbar {
    width: 6px;
  }

  .body-scroll::-webkit-scrollbar-thumb {
    background: var(--fg);
  }

  .body-text {
    margin: 0;
    font-family: var(--font-mono);
    font-size: 0.88rem;
    line-height: 1.6;
    color: var(--fg);
    white-space: pre-wrap;
    word-break: break-word;
  }

  /* ── Actions ──────────────────────────────────────────────────── */

  .actions {
    display: flex;
    gap: 0.75rem;
    justify-content: flex-end;
    flex-wrap: wrap;
    flex-shrink: 0;
  }

  .action {
    font-family: var(--font-mono);
    font-size: 0.85rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--fg);
    background: var(--bg);
    border: 2px solid var(--fg);
    padding: 0.55rem 1rem;
    cursor: pointer;
    /* Flat-by-default, pop-on-hover: button lifts up-left and casts a
       hard offset shadow; on active it lands flat again. */
    transform: translate(0, 0);
    transition:
      transform 0.08s ease,
      box-shadow 0.08s ease;
  }

  .action:hover {
    transform: translate(-2px, -2px);
    box-shadow: 4px 4px 0 0 var(--fg);
  }

  .action:active {
    transform: translate(0, 0);
    box-shadow: 0 0 0 0 var(--fg);
  }

  .action:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* Variant fills -- semantically coloured per cluster palette. */
  .action--keep {
    background: var(--yellow);
  }

  .action--reject {
    background: var(--coral);
  }

  .action--agree {
    background: var(--green);
  }

  .action--danger {
    background: var(--coral);
  }
</style>
