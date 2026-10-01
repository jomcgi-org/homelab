<script>
  // Inline runs from markdown.js, painted through text nodes only. Strong,
  // emphasis and links nest, so the component renders itself for their
  // children; no run ever becomes markup.
  import Inline from "./Inline.svelte";

  let { runs = [] } = $props();
</script>

{#each runs as run, index (index)}{#if run.br}<br
    />{:else if run.box != null}<span class="box" aria-hidden="true"
      >{run.box ? "■" : "□"}</span
    >{:else if run.code != null}<code>{run.code}</code
    >{:else if run.strong}<strong><Inline runs={run.strong} /></strong
    >{:else if run.em}<em><Inline runs={run.em} /></em>{:else if run.del}<s
      ><Inline runs={run.del} /></s
    >{:else if run.inline}{#if run.link}<a href={run.link} rel="noopener"
        ><Inline runs={run.inline} /></a
      >{:else}<Inline runs={run.inline} />{/if}{:else}{run.text}{/if}{/each}
