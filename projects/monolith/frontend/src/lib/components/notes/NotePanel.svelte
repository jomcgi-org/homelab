<script>
  import { renderMarkdown } from "./markdown.js";
  import { colorFor, labelFor } from "./clusters.js";

  let {
    selectedId,
    nodes,
    edges,
    onSelect,
    onClose,
    apiBase = "/api/knowledge/notes",
  } = $props();

  let byId = $derived(new Map(nodes.map((n) => [n.id, n])));
  let titleMap = $derived(new Map(nodes.map((n) => [n.title, { id: n.id }])));
  let selectedNode = $derived(byId.get(selectedId));

  // Dedupe per column: a single source/target pair can have multiple
  // edges (one body wikilink + one frontmatter `refines`, etc.) — they
  // shouldn't show as duplicate rows in BACKLINKS / OUTGOING. The same
  // node CAN still appear in both columns (bidirectional reference),
  // which is the meaningful signal.
  function uniqueByNode(edgeStream, idField) {
    const seen = new Set();
    const out = [];
    for (const e of edgeStream) {
      const id = e[idField];
      if (seen.has(id)) continue;
      const node = byId.get(id);
      if (!node) continue;
      seen.add(id);
      out.push(node);
    }
    return out;
  }
  let backlinks = $derived(
    uniqueByNode(
      edges.filter((e) => e.target === selectedId),
      "source",
    ),
  );
  let outgoing = $derived(
    uniqueByNode(
      edges.filter((e) => e.source === selectedId),
      "target",
    ),
  );

  let body = $state("");
  let loading = $state(false);
  let error = $state("");

  // Strip YAML frontmatter and the trailing links/related section.
  // Both are already represented in the panel — the type chip + dot
  // express what frontmatter would say, and BACKLINKS/OUTGOING in the
  // foot capture what `## links` would. Rendering them again in body
  // is duplication.
  function trimNoteBody(md) {
    // Drop leading `---\n…\n---\n` YAML block.
    let s = md.replace(/^---\s*\n[\s\S]*?\n---\s*\n+/, "");
    // Drop trailing `## links` / `## related` / `## related links`
    // section through end of document. Case-insensitive, optional
    // pluralisation.
    s = s.replace(/\n##+\s+(?:related\s+)?links?\b[^\n]*\n[\s\S]*$/i, "");
    s = s.replace(/\n##+\s+related\b[^\n]*\n[\s\S]*$/i, "");
    return s.trimEnd();
  }

  let trimmedBody = $derived(trimNoteBody(body));

  $effect(() => {
    if (!selectedId) {
      body = "";
      error = "";
      loading = false;
      return;
    }
    const controller = new AbortController();
    loading = true;
    error = "";
    body = "";
    fetch(`${apiBase}/${encodeURIComponent(selectedId)}`, {
      signal: controller.signal,
    })
      .then((r) =>
        r.ok ? r.json() : Promise.reject(new Error("fetch failed")),
      )
      .then((data) => {
        // Private endpoint returns `content` (raw markdown incl. frontmatter,
        // trimmed client-side); public endpoint returns `body` (already
        // stripped + wikilink-sanitized server-side). Accept either.
        body = data.content ?? data.body ?? "";
      })
      .catch((e) => {
        if (e.name !== "AbortError") error = "couldn't load note body";
      })
      .finally(() => {
        if (!controller.signal.aborted) loading = false;
      });
    return () => controller.abort();
  });

  function handleBodyClick(e) {
    const a = e.target.closest("a.wl[data-id]");
    if (!a) return;
    e.preventDefault();
    onSelect(a.dataset.id);
  }
</script>

{#if selectedNode}
  <aside class="panel">
    <div class="panel-head">
      <span class="panel-dot" style:background={colorFor(selectedNode.type)}
      ></span>
      <div class="panel-titlewrap">
        <div class="panel-eyebrow">
          {labelFor(selectedNode.type).toUpperCase()}
        </div>
        <div class="panel-title">{selectedNode.title}</div>
      </div>
      <button class="panel-close" onclick={onClose} aria-label="close">×</button
      >
    </div>

    <div
      class="panel-body"
      onclick={handleBodyClick}
      onkeydown={(e) => {
        if (e.key === "Enter" || e.key === " ") handleBodyClick(e);
      }}
      role="presentation"
    >
      {#if loading}
        <p class="panel-loading">loading…</p>
      {:else if error}
        <p class="panel-error">{error}</p>
      {:else}
        {@html renderMarkdown(trimmedBody, titleMap)}
      {/if}
    </div>

    <div class="panel-foot">
      <div>
        <h5>BACKLINKS</h5>
        <ul class="link-list">
          {#each backlinks.slice(0, 10) as nb}
            <li>
              <button
                type="button"
                class="link-row"
                onclick={() => onSelect(nb.id)}
              >
                <span class="swatch" style:background={colorFor(nb.type)}
                ></span>
                <span>{nb.title}</span>
              </button>
            </li>
          {/each}
          {#if backlinks.length === 0}
            <li class="empty">— none</li>
          {/if}
          {#if backlinks.length > 10}
            <li class="more">+ {backlinks.length - 10} more</li>
          {/if}
        </ul>
      </div>
      <div>
        <h5>OUTGOING</h5>
        <ul class="link-list">
          {#each outgoing.slice(0, 10) as nb}
            <li>
              <button
                type="button"
                class="link-row"
                onclick={() => onSelect(nb.id)}
              >
                <span class="swatch" style:background={colorFor(nb.type)}
                ></span>
                <span>{nb.title}</span>
              </button>
            </li>
          {/each}
          {#if outgoing.length === 0}
            <li class="empty">— none</li>
          {/if}
          {#if outgoing.length > 10}
            <li class="more">+ {outgoing.length - 10} more</li>
          {/if}
        </ul>
      </div>
    </div>
  </aside>
{/if}

<style>
  .panel {
    position: absolute;
    top: 20px;
    right: 20px;
    /* Wider than the original 440px so ASCII diagrams, code fences,
       and tables breathe. Scales with viewport but capped so it
       doesn't dominate the canvas on big screens. */
    width: min(640px, 45vw);
    max-height: calc(100% - 40px);
    background: #ffffff;
    border: 1.5px solid #141414;
    box-shadow: 4px 4px 0 #141414;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    z-index: 6;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
  }
  .panel-head {
    padding: 14px 16px;
    border-bottom: 1.5px dashed #141414;
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }
  .panel-dot {
    width: 12px;
    height: 12px;
    border: 1.5px solid #141414;
    margin-top: 4px;
    flex-shrink: 0;
  }
  .panel-titlewrap {
    flex: 1;
    min-width: 0;
  }
  .panel-eyebrow {
    font-size: 9px;
    letter-spacing: 0.14em;
    color: #8a857a;
    margin-bottom: 3px;
  }
  .panel-title {
    font-size: 14px;
    font-weight: 500;
    word-break: break-word;
  }
  .panel-close {
    background: #f1ebdc;
    border: 1.5px solid #141414;
    width: 22px;
    height: 22px;
    cursor: pointer;
    padding: 0;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 12px;
    line-height: 1;
  }
  .panel-close:hover {
    background: #141414;
    color: #ffffff;
  }
  .panel-body {
    padding: 14px 18px 18px;
    overflow-y: auto;
    font-size: 12.5px;
    line-height: 1.55;
  }
  .panel-body :global(h2) {
    font-size: 11px;
    letter-spacing: 0.16em;
    font-weight: 700;
    margin: 16px 0 8px;
    text-transform: uppercase;
    border-top: 1px dashed #141414;
    padding-top: 10px;
  }
  .panel-body :global(h2:first-child) {
    border-top: none;
    padding-top: 0;
    margin-top: 0;
  }
  .panel-body :global(h3) {
    font-size: 11px;
    letter-spacing: 0.1em;
    font-weight: 700;
    margin: 12px 0 6px;
    color: #8a857a;
  }
  .panel-body :global(p) {
    margin: 0 0 10px;
  }
  .panel-body :global(ul) {
    margin: 0 0 10px;
    padding-left: 18px;
    /* global.css resets list-style to none on every ul; restore it
       here so note bullets render normally. */
    list-style: disc;
  }
  .panel-body :global(ol) {
    margin: 0 0 10px;
    padding-left: 22px;
    list-style: decimal;
  }
  .panel-body :global(li) {
    margin-bottom: 3px;
  }
  .panel-body :global(table) {
    border-collapse: collapse;
    width: 100%;
    margin: 10px 0;
    font-size: 11.5px;
  }
  .panel-body :global(th),
  .panel-body :global(td) {
    border: 1px solid #141414;
    padding: 4px 8px;
    text-align: left;
    vertical-align: top;
  }
  .panel-body :global(th) {
    background: #f1ebdc;
    font-weight: 700;
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .panel-body :global(code) {
    background: #f1ebdc;
    padding: 1px 5px;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 11.5px;
    border: 1px solid rgba(0, 0, 0, 0.15);
  }
  .panel-body :global(pre) {
    background: #f1ebdc;
    border: 1px solid #141414;
    padding: 10px 12px;
    margin: 10px 0;
    overflow-x: auto;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 11px;
    line-height: 1.4;
    white-space: pre;
  }
  .panel-body :global(pre code) {
    /* Inside <pre>, the inline-code chip styling is wrong — strip it. */
    background: transparent;
    border: none;
    padding: 0;
    font-size: inherit;
  }
  .panel-body :global(strong) {
    font-weight: 700;
  }
  .panel-body :global(blockquote) {
    margin: 10px 0;
    padding: 8px 12px;
    border-left: 3px solid #141414;
    background: #f1ebdc;
  }
  .panel-body :global(a.wl) {
    color: #141414;
    text-decoration: underline;
    text-decoration-color: #ff6b5b;
    text-underline-offset: 2px;
    cursor: pointer;
  }
  .panel-body :global(a.wl:hover) {
    background: #f5d90a;
  }
  .panel-body :global(a.wl.dead) {
    color: #8a857a;
    text-decoration-style: dotted;
    text-decoration-color: #8a857a;
    cursor: default;
  }
  .panel-body :global(.tag) {
    display: inline-block;
    font-size: 10px;
    padding: 1px 6px;
    border: 1px solid #141414;
    background: #ffffff;
    margin-right: 4px;
  }
  .panel-loading,
  .panel-error {
    color: #8a857a;
    font-style: italic;
  }
  .panel-error {
    color: #cc0000;
  }
  .panel-foot {
    border-top: 1.5px dashed #141414;
    padding: 12px 16px;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    font-size: 11px;
  }
  .panel-foot h5 {
    margin: 0 0 6px;
    font-size: 9px;
    letter-spacing: 0.14em;
    color: #8a857a;
    font-weight: 700;
  }
  .link-list {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  .link-list li {
    padding: 0;
    font-size: 11px;
  }
  .link-list .link-row {
    all: unset;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 7px;
    width: 100%;
    padding: 3px 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: inherit;
    font-size: inherit;
    color: inherit;
  }
  .link-list .link-row:hover {
    color: #ff6b5b;
  }
  .link-list .link-row:focus-visible {
    outline: 1.5px solid #141414;
    outline-offset: 2px;
  }
  .link-list .swatch {
    width: 8px;
    height: 8px;
    border: 1.2px solid #141414;
    flex-shrink: 0;
  }
  .link-list .empty,
  .link-list .more {
    color: #8a857a;
    font-size: 10px;
    cursor: default;
  }
</style>
