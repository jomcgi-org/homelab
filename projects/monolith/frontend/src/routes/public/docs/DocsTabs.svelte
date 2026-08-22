<script>
  import { DOC_KINDS } from "$lib/public/docs/doc-kinds.js";

  /**
   * @type {{
   *   tabs: {kind:string,label:string,slug:string|null,title:string,disabled?:boolean}[],
   *   activeKind?: string,
   *   compact?: boolean,
   * }}
   */
  let { tabs, activeKind = "", compact = false } = $props();

  const tabByKind = $derived(new Map(tabs.map((tab) => [tab.kind, tab])));
</script>

<nav class="doc-tabs" class:compact aria-label="Project documents">
  {#each DOC_KINDS as definition}
    {@const tab = tabByKind.get(definition.kind)}
    {#if tab?.slug && !tab.disabled}
      <a
        class="doc-tab"
        class:active={activeKind === definition.kind}
        aria-current={activeKind === definition.kind ? "page" : undefined}
        href={`/docs/${tab.slug}`}
        title={tab.title}>{definition.label}</a
      >
    {:else}
      <span class="doc-tab disabled" aria-disabled="true"
        >{definition.label}</span
      >
    {/if}
  {/each}
</nav>

<style>
  .doc-tabs {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    margin: 0 0 24px;
  }

  .doc-tab {
    display: inline-block;
    padding: 5px 9px;
    border: 2px solid var(--ink);
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 600;
    line-height: 1.2;
    color: var(--ink-2);
    background: var(--paper);
    text-decoration: none;
    white-space: nowrap;
  }

  a.doc-tab:hover,
  .doc-tab.active {
    color: var(--ink);
    background: var(--accent);
  }

  .doc-tab.active {
    box-shadow: 3px 3px 0 var(--ink);
  }

  .doc-tab.disabled {
    color: var(--ink-3);
    border-color: var(--rule-2);
    background: var(--bg-elev);
    cursor: not-allowed;
  }

  .doc-tabs.compact {
    gap: 4px;
    margin: 6px 0 4px;
  }

  .compact .doc-tab {
    padding: 3px 5px;
    border-width: 1px;
    font-size: 9px;
  }

  .compact .doc-tab.active {
    box-shadow: 2px 2px 0 var(--ink);
  }
</style>
