<script>
  import "$lib/grimoire/theme.css";
  import { invalidateAll } from "$app/navigation";
  import GrantEditor from "$lib/grimoire/GrantEditor.svelte";
  let { data } = $props();
  let query = $state("");
  let type = $state("");
  let session = $state("");
  let selection = $state(null);
  let types = $derived(
    [...new Set(data.entities.map((entity) => entity.entity_type))].sort(),
  );
  let entities = $derived(
    data.entities.filter(
      (entity) =>
        entity.name.toLowerCase().includes(query.toLowerCase()) &&
        (!type || entity.entity_type === type) &&
        (!session ||
          data.grants.some(
            (grant) =>
              grant.entity_id === entity.id &&
              grant.granted_in_session === session,
          )),
    ),
  );
  const grantFor = (entity, pc) =>
    data.grants.find(
      (grant) =>
        grant.entity_id === entity.id && grant.player_character_id === pc.id,
    );
  async function saved() {
    selection = null;
    await invalidateAll();
  }
</script>

<svelte:head><title>Knowledge grants · {data.campaign.name}</title></svelte:head
>
<main class="grimoire grants">
  <a href={`/grimoire/campaigns/${data.campaign.id}/session`}
    >← Back to the table</a
  >
  <h1>Knowledge at your table</h1>
  <p>{data.campaign.name}</p>
  <div class="filters">
    <label>Find knowledge<input bind:value={query} /></label><label
      >Entity type<select bind:value={type}
        ><option value="">All types</option>{#each types as item}<option
            value={item}>{item}</option
          >{/each}</select
      ></label
    ><label
      >Granted in session<select bind:value={session}
        ><option value="">All sessions</option
        >{#each data.sessions as item}<option value={item.id}
            >{new Date(item.started_at).toLocaleDateString()} · {item.status}</option
          >{/each}</select
      ></label
    >
  </div>
  {#if selection}{#key `${selection.entity.id}:${selection.pc.id}`}<GrantEditor
        endpoint={`/grimoire/campaigns/${data.campaign.id}/session/state`}
        entityId={selection.entity.id}
        character={selection.pc}
        grant={grantFor(selection.entity, selection.pc)}
        {saved}
        cancel={() => (selection = null)}
      />{/key}{/if}
  <div class="matrix">
    <table>
      <caption
        >Choose a character's scope to edit or retract their knowledge.</caption
      ><thead
        ><tr
          ><th>Knowledge</th>{#each data.characters as pc}<th
              >{pc.character_name}</th
            >{/each}</tr
        ></thead
      ><tbody
        >{#each entities as entity}<tr
            ><th>{entity.name}<small>{entity.entity_type}</small></th
            >{#each data.characters as pc}<td
                ><button
                  aria-label={`Edit ${entity.name} for ${pc.character_name}`}
                  onclick={() => (selection = { entity, pc })}
                  >{grantFor(entity, pc)?.grant_scope?.replaceAll("_", " ") ||
                    (entity.is_global
                      ? "Full by default"
                      : "Not granted")}</button
                ></td
              >{/each}</tr
          >{/each}</tbody
      >
    </table>
  </div>
</main>

<style>
  .grants {
    max-width: 1100px;
    margin: 40px auto;
    padding: 0 20px;
  }
  .filters {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    margin: 24px 0;
  }
  label {
    display: grid;
    gap: 8px;
  }
  input,
  select,
  button {
    font: inherit;
    padding: 10px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    color: var(--grim-ink);
  }
  button {
    cursor: pointer;
  }
  .matrix {
    overflow-x: auto;
  }
  table {
    width: 100%;
    border-collapse: collapse;
  }
  th,
  td {
    text-align: left;
    padding: 12px;
    border-bottom: 1px solid var(--grim-line);
  }
  caption {
    text-align: left;
    color: var(--grim-ink-soft);
    padding: 12px 0;
  }
  small {
    display: block;
    color: var(--grim-ink-soft);
  }
</style>
