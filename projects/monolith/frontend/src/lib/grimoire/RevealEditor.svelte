<script>
  import { knowledgeFields, selectedDetails } from "./knowledge-fields.js";
  import RevealProjection from "./RevealProjection.svelte";
  let { endpoint, characters, changed } = $props();
  let query = $state("");
  let recipient = $state("");
  let selected = $state([]);
  let items = $state([]);
  let entity = $state(null);
  let scope = $state("name_only");
  let clue = $state("");
  let fields = $state([]);
  let previews = $state([]);
  let previewSignature = $state("");
  let busy = $state(false);
  let failure = $state("");
  let notice = $state("");
  let silent = $state(false);
  let revealedDetails = $derived(selectedDetails(entity, fields, clue));
  let signature = $derived(
    JSON.stringify({ entityId: entity?.id, selected, scope, revealedDetails }),
  );

  async function choose(item) {
    busy = true;
    failure = "";
    try {
      const response = await fetch(`${endpoint}?entity=${item.id}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      entity = result;
      selected = [];
      fields = [];
      previews = [];
      previewSignature = "";
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }

  async function search(event) {
    event?.preventDefault();
    busy = true;
    failure = "";
    try {
      const params = new URLSearchParams({ q: query });
      if (recipient) params.set("notGrantedTo", recipient);
      const response = await fetch(`${endpoint}?${params}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      items = result.items;
      entity = null;
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }

  async function save(operation, grantId) {
    const requestedSignature = signature;
    busy = true;
    failure = "";
    notice = "";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          operation,
          entityId: entity.id,
          pcIds: selected,
          scope,
          clue,
          revealedDetails,
          grantId,
          silent,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (operation === "previewReveal") {
        previews = result;
        previewSignature = requestedSignature;
        return;
      }
      notice =
        operation === "reveal" ? "Knowledge shared." : "Knowledge retracted.";
      await changed();
      await search();
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }
</script>

<div class="reveal-editor">
  <form onsubmit={search}>
    <label
      >Find knowledge<input
        bind:value={query}
        placeholder="NPC, place, item…"
      /></label
    >
    <label
      >Hide knowledge already granted to<select bind:value={recipient}
        ><option value="">Show all</option>{#each characters as pc}<option
            value={pc.id}>{pc.character_name}</option
          >{/each}</select
      ></label
    >
    <button disabled={busy}>Search knowledge</button>
  </form>
  <div class="results">
    {#each items as item}<button
        class="choice"
        disabled={busy}
        onclick={() => choose(item)}
        >{item.name} <small>{item.entity_type}</small></button
      >{/each}
  </div>
  {#if entity}
    <h3>{entity.name}</h3>
    <fieldset>
      <legend>Share with</legend>{#each characters as pc}<label class="check"
          ><input
            type="checkbox"
            bind:group={selected}
            value={pc.id}
            disabled={entity.grants?.some(
              (grant) => grant.player_character_id === pc.id,
            )}
          />{pc.character_name}</label
        >{/each}
    </fieldset>
    <label
      >Knowledge scope<select bind:value={scope}
        ><option value="name_only">Name only</option><option value="partial"
          >Selected detail</option
        ><option value="full">Full knowledge</option></select
      ></label
    >
    {#if scope === "partial"}<label
        >Detail to share<textarea bind:value={clue} maxlength="8000" rows="3"
        ></textarea></label
      >{/if}
    {#if scope === "partial"}<fieldset>
        <legend>Fields to share</legend
        >{#each knowledgeFields(entity) as [key]}<label class="check"
            ><input
              type="checkbox"
              bind:group={fields}
              value={key}
            />{key.replaceAll("_", " ")}</label
          >{/each}
      </fieldset>{/if}
    <p class="preview">
      Players will see {entity.name}{scope === "partial"
        ? ` and: ${clue || "your selected detail"}`
        : scope === "full"
          ? " and all its knowledge details."
          : " and its type only."}
    </p>
    <button
      disabled={busy ||
        !selected.length ||
        (scope === "partial" && !Object.keys(revealedDetails).length)}
      onclick={() => save("previewReveal")}>Preview knowledge</button
    >
    {#if previews.length && previewSignature === signature}<div
        aria-label="Recipient previews"
      >
        {#each previews as preview}<h4>
            {characters.find((pc) => pc.id === preview.player_character_id)
              ?.character_name}
          </h4>
          <RevealProjection
            knowledge={{
              projection: preview.projection,
              grant_scope: scope,
            }}
          />{/each}
      </div>
      <button disabled={busy} onclick={() => save("reveal")}
        >Share knowledge</button
      >{/if}
    {#if entity.grants?.length}
      <label class="check"
        ><input type="checkbox" bind:checked={silent} />Retract silently</label
      >
      {#each entity.grants as grant}<button
          class="choice"
          disabled={busy}
          onclick={() => save("revoke", grant.id)}
          >Retract from {characters.find(
            (pc) => pc.id === grant.player_character_id,
          )?.character_name || "character"}</button
        >{/each}
    {/if}
  {/if}
  {#if failure}<p role="alert">{failure}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
</div>

<style>
  .reveal-editor {
    padding-top: 8px;
  }
  form,
  label {
    display: grid;
    gap: 8px;
  }
  form,
  .results {
    margin-bottom: 16px;
  }
  input,
  select,
  textarea,
  button {
    font: inherit;
    color: var(--grim-ink);
    border: 1px solid var(--grim-line);
    padding: 10px;
    background: var(--grim-surface);
    box-sizing: border-box;
    width: 100%;
  }
  button {
    cursor: pointer;
    margin-top: 8px;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .check {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 12px 0;
  }
  .check input {
    width: auto;
  }
  fieldset {
    border: 1px solid var(--grim-line);
    margin-bottom: 12px;
  }
  small,
  .preview {
    color: var(--grim-ink-soft);
  }
</style>
