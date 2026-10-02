<script>
  import NotesPanel from "$lib/grimoire/NotesPanel.svelte";

  let { data, form } = $props();
  const filters = $derived(
    new URLSearchParams({ kind: data.kind, q: data.q }).toString(),
  );
</script>

<svelte:head><title>{data.campaign.name} notes · Grimoire</title></svelte:head>

<main>
  <a href="/grimoire">Back to campaigns</a>
  <h1>{data.campaign.name} notes</h1>
  {#key data.campaign.id}
    <NotesPanel
      campaignId={data.campaign.id}
      notes={data.notes}
      initialKind={data.kind}
      query={data.q}
      isDm={data.campaign.role === "dm"}
      canCreate={data.campaign.role === "dm" ||
        !!data.campaign.player_character_id}
      error={form?.error}
      saved={form?.ok}
      createAction={`?${filters}&/create`}
      updateAction={`?${filters}&/update`}
      deleteAction={`?${filters}&/delete`}
    />
  {/key}
</main>

<style>
  main {
    max-width: 900px;
    margin: 0 auto;
    padding: 2rem 1rem;
  }
  h1 {
    margin: 1.5rem 0;
  }
</style>
