<script>
  let { nodes = [], onNodeClick } = $props();
</script>

{#each nodes as node}<button onclick={() => onNodeClick?.(node)}
    >Select {node.title}</button
  >{/each}
