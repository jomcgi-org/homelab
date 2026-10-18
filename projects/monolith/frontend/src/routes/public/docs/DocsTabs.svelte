<script>
  import { DOC_KINDS } from "$lib/public/docs/doc-kinds.js";

  /**
   * The documents a project publishes, as a tab strip for a record's head
   * band: cells divided by rules, the current one underlined. Only kinds
   * that exist render; an absent document is nothing to draw.
   * @type {{
   *   tabs: {kind:string,label:string,slug:string|null,title:string,disabled?:boolean}[],
   *   activeKind?: string,
   * }}
   */
  let { tabs, activeKind = "" } = $props();

  const tabByKind = $derived(new Map(tabs.map((tab) => [tab.kind, tab])));
  const published = $derived(
    DOC_KINDS.flatMap(({ kind, label }) => {
      const tab = tabByKind.get(kind);
      return tab?.slug && !tab.disabled ? [{ ...tab, kind, label }] : [];
    }),
  );
</script>

<nav class="tabs" aria-label="Project documents">
  {#each published as tab (tab.kind)}
    <a
      class:here={activeKind === tab.kind}
      aria-current={activeKind === tab.kind ? "page" : undefined}
      href={`/docs/${tab.slug}`}
      title={tab.title}>{tab.label}</a
    >
  {/each}
</nav>
