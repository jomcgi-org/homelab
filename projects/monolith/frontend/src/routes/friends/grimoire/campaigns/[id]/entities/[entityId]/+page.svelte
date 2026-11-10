<script>
  import GrimoireSheet from "$lib/grimoire/GrimoireSheet.svelte";
  import KnowledgeDetails from "$lib/grimoire/KnowledgeDetails.svelte";
  let { data } = $props();
</script>

<svelte:head><title>{data.entity.name} · Grimoire</title></svelte:head>
<GrimoireSheet current={data.entity.name}>
  <div class="knowledge edition ed-lead">
    <a href={`/grimoire/campaigns/${data.campaignId}/session`}
      >← Back to the table</a
    >
    <p class="eyebrow">{data.entity.entity_type}</p>
    <h1>{data.entity.name}</h1>
    <KnowledgeDetails entity={data.entity} />
  </div>
</GrimoireSheet>

<style>
  .knowledge {
    min-width: 0;
  }
</style>
