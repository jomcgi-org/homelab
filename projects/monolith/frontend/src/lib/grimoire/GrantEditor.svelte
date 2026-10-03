<script>
  import { onMount } from "svelte";
  import { knowledgeFields, selectedDetails } from "./knowledge-fields.js";
  import KnowledgeDetails from "./KnowledgeDetails.svelte";
  let { endpoint, entityId, character, grant, saved, cancel } = $props();
  let entity = $state(null);
  let scope = $state("name_only");
  let fields = $state([]);
  let clue = $state("");
  let preview = $state(null);
  let previewSignature = $state("");
  let busy = $state(false);
  let failure = $state("");
  let silent = $state(false);
  let details = $derived(selectedDetails(entity, fields, clue));
  let signature = $derived(JSON.stringify({ scope, details }));
  onMount(async () => {
    scope = grant?.grant_scope || "name_only";
    clue = grant?.revealed_details?.clue || "";
    fields = Object.keys(grant?.revealed_details || {}).filter(
      (key) => key !== "clue",
    );
    try {
      const response = await fetch(`${endpoint}?entity=${entityId}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      entity = result;
      if (!grant && entity.is_global) scope = "full";
    } catch (error) {
      failure = error.message;
    }
  });
  async function act(operation) {
    busy = true;
    failure = "";
    const requestedSignature = signature;
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          operation,
          entityId,
          pcIds: [character.id],
          scope,
          revealedDetails: details,
          grantId: grant?.id,
          silent,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (operation === "previewReveal") {
        preview = result[0].projection;
        previewSignature = requestedSignature;
      } else await saved();
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }
</script>

<section class="editor" aria-label="Edit character knowledge">
  <h2>{entity?.name || "Knowledge"} · {character.character_name}</h2>
  <button onclick={cancel}>Close editor</button>
  {#if entity}
    <label
      >Knowledge scope<select bind:value={scope} aria-label="Knowledge scope"
        ><option value="name_only">Name only</option><option value="partial"
          >Selected detail</option
        ><option value="full">Full knowledge</option></select
      ></label
    >
    {#if scope === "partial"}<fieldset>
        <legend>Fields to share</legend
        >{#each knowledgeFields(entity) as [key]}<label class="check"
            ><input
              type="checkbox"
              bind:group={fields}
              value={key}
            />{key.replaceAll("_", " ")}</label
          >{/each}
      </fieldset>
      <label
        >Detail to share<textarea bind:value={clue} rows="3" maxlength="8000"
        ></textarea></label
      >{/if}
    <button
      disabled={busy || (scope === "partial" && !Object.keys(details).length)}
      onclick={() => act("previewReveal")}>Preview knowledge</button
    >
    {#if preview && previewSignature === signature}<h3>
        What {character.character_name} will see
      </h3>
      <p>{preview.name} · {preview.entity_type}</p>
      <KnowledgeDetails entity={preview} /><button
        disabled={busy}
        onclick={() => act(grant ? "updateGrant" : "reveal")}
        >Confirm knowledge</button
      >{/if}
    {#if grant}<label class="check"
        ><input type="checkbox" bind:checked={silent} />Retract silently</label
      ><button disabled={busy} onclick={() => act("revoke")}
        >Retract knowledge</button
      >{/if}
  {/if}
  {#if failure}<p role="alert">{failure}</p>{/if}
</section>

<style>
  .editor {
    border: 1px solid var(--grim-line);
    padding: 20px;
    margin: 24px 0;
  }
  label {
    display: grid;
    gap: 8px;
    margin: 16px 0;
  }
  input,
  textarea,
  select,
  button {
    font: inherit;
    padding: 10px;
    color: var(--grim-ink);
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    box-sizing: border-box;
  }
  textarea {
    width: 100%;
  }
  button {
    cursor: pointer;
    margin: 8px 8px 8px 0;
  }
  button:disabled {
    opacity: 0.5;
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  fieldset {
    border: 1px solid var(--grim-line);
  }
</style>
