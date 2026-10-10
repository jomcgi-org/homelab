<script>
  import "$lib/grimoire/theme.css";
  import KnowledgeDetails from "$lib/grimoire/KnowledgeDetails.svelte";
  let { data } = $props();
</script>

<svelte:head><title>{data.entity.name} · Grimoire</title></svelte:head>
<main class="grimoire knowledge">
  <a href={`/grimoire/campaigns/${data.campaignId}/session`}
    >← Back to the table</a
  >
  <p class="eyebrow">{data.entity.entity_type}</p>
  <h1>{data.entity.name}</h1>
  <KnowledgeDetails entity={data.entity} />
</main>

<style>
  .knowledge {
    max-width: 760px;
    margin: 40px auto;
    padding: 0 20px;
  }
</style>
