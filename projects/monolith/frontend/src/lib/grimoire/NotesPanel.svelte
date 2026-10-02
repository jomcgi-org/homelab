<script>
  import { untrack } from "svelte";
  import Markdown from "$lib/public/factory/Markdown.svelte";

  let {
    campaignId,
    notes = [],
    initialKind = "character",
    query = "",
    isDm = false,
    canCreate = true,
    error = null,
    saved = false,
    filterAction = "",
    createAction = "?/create",
    updateAction = "?/update",
    deleteAction = "?/delete",
  } = $props();
  const panelId = $props.id();
  let kind = $state(untrack(() => initialKind));
  let shareChoice = $state("default");
  $effect(() => {
    kind = initialKind;
  });
  const visibleNotes = $derived(notes.filter((note) => note.kind === kind));
</script>

<section class="notes" aria-label="Campaign notes">
  {#if error}<p role="alert">{error}</p>
  {:else if saved}<p role="status">Saved.</p>{/if}

  <form method="GET" action={filterAction}>
    <input type="hidden" name="q" value={query} />
    <div role="tablist" aria-label="Note audience">
      <button
        type="submit"
        role="tab"
        id={`${panelId}-character`}
        aria-controls={`${panelId}-notes`}
        aria-selected={kind === "character"}
        name="kind"
        value="character"
        onclick={() => {
          kind = "character";
        }}
      >
        {isDm ? "Shared with you" : "Mine"}
      </button>
      <button
        type="submit"
        role="tab"
        id={`${panelId}-party`}
        aria-controls={`${panelId}-notes`}
        aria-selected={kind === "party"}
        name="kind"
        value="party"
        onclick={() => {
          kind = "party";
        }}>Party</button
      >
    </div>
  </form>

  <form method="GET" action={filterAction} class="search">
    <input type="hidden" name="kind" value={kind} />
    <label
      >Search notes <input
        type="search"
        name="q"
        value={query}
        maxlength="20000"
      /></label
    >
    <button>Search</button>
  </form>

  <div
    role="tabpanel"
    id={`${panelId}-notes`}
    aria-labelledby={`${panelId}-${kind}`}
  >
    {#if canCreate && (!isDm || kind === "party")}
      {#key kind}
        <form
          method="POST"
          action={createAction}
          class="editor"
          aria-label="Add note"
        >
          <input type="hidden" name="campaign_id" value={campaignId} />
          <input type="hidden" name="kind" value={kind} />
          <label>Title <input name="title" required maxlength="200" /></label>
          <label
            >Markdown <textarea name="markdown" rows="5" maxlength="20000"
            ></textarea></label
          >
          {#if kind === "character"}
            <label
              >DM sharing
              <select bind:value={shareChoice}>
                <option value="default">Campaign default</option>
                <option value="false">Private</option>
                <option value="true">Share with DM</option>
              </select>
            </label>
            {#if shareChoice !== "default"}<input
                type="hidden"
                name="dm_readable"
                value={shareChoice}
              />{/if}
          {:else}<p>
              Party notes are shared with the DM and players with a character.
            </p>{/if}
          <button>Add note</button>
        </form>
      {/key}
    {/if}

    {#if !visibleNotes.length}<p>
        No {kind === "party"
          ? "party notes"
          : isDm
            ? "notes shared with you"
            : "personal notes"}{query ? " match your search" : " yet"}.
      </p>{/if}
    {#each visibleNotes as note (note.id)}
      <article aria-label={note.title}>
        <h2>{note.title}</h2>
        {#if note.kind === "character" && note.is_mine}<p class="sharing">
            {note.dm_readable ? "Shared with DM" : "Private"}
          </p>{/if}
        <Markdown text={note.markdown} />
        {#if note.links?.entities?.length}
          <ul class="chips" aria-label="Linked entities">
            {#each note.links.entities as entity (entity.id)}<li>
                {entity.name}
              </li>{/each}
          </ul>
        {/if}
        {#if note.can_edit}
          <details>
            <summary>Edit note</summary>
            <form
              method="POST"
              action={updateAction}
              class="editor"
              aria-label={`Edit ${note.title}`}
            >
              <input type="hidden" name="campaign_id" value={campaignId} />
              <input type="hidden" name="note_id" value={note.id} />
              <label
                >Title <input
                  name="title"
                  value={note.title}
                  required
                  maxlength="200"
                /></label
              >
              <label
                >Markdown <textarea
                  name="markdown"
                  rows="5"
                  maxlength="20000"
                  value={note.markdown}></textarea></label
              >
              {#if note.kind === "character" && note.is_mine}
                <label
                  >DM sharing
                  <select name="dm_readable" value={String(note.dm_readable)}>
                    <option value="false">Private</option>
                    <option value="true">Share with DM</option>
                  </select>
                </label>
              {/if}
              <button>Save changes</button>
            </form>
            <form method="POST" action={deleteAction}>
              <input type="hidden" name="campaign_id" value={campaignId} />
              <input type="hidden" name="note_id" value={note.id} />
              <button class="delete">Delete note</button>
            </form>
          </details>
        {/if}
      </article>
    {/each}
  </div>
</section>

<style>
  .notes {
    display: grid;
    gap: 1.5rem;
  }
  [role="tablist"],
  .search {
    display: flex;
    gap: 0.75rem;
    flex-wrap: wrap;
    align-items: end;
  }
  button,
  input,
  textarea,
  select {
    font: inherit;
  }
  button {
    padding: 0.55rem 0.85rem;
    cursor: pointer;
  }
  [aria-selected="true"] {
    font-weight: 700;
    border-bottom: 3px solid var(--grim-accent, #33507a);
  }
  .editor {
    display: grid;
    gap: 0.85rem;
    margin-bottom: 1rem;
  }
  label {
    display: grid;
    gap: 0.35rem;
  }
  input,
  textarea,
  select {
    padding: 0.55rem;
    max-width: 100%;
    box-sizing: border-box;
  }
  textarea {
    resize: vertical;
  }
  .editor button {
    justify-self: start;
  }
  article {
    margin-top: 1.5rem;
    padding: 1.25rem;
    border: 1px solid var(--border, #b5bcc7);
    background: var(--grim-paper, #f3f5f7);
    overflow-wrap: anywhere;
  }
  h2 {
    margin: 0 0 0.75rem;
    font-family: Georgia, serif;
  }
  .sharing {
    font-size: 0.9rem;
  }
  .chips {
    list-style: none;
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    padding: 0;
  }
  .chips li {
    border: 1px solid var(--border, #b5bcc7);
    padding: 0.2rem 0.5rem;
  }
  summary {
    cursor: pointer;
    margin: 1rem 0;
  }
  .delete {
    color: var(--text, #222);
  }
  [role="alert"] {
    border-left: 3px solid #a52525;
    padding-left: 0.75rem;
  }
  :is(button, input, textarea, select, summary):focus-visible {
    outline: 2px solid var(--grim-accent, #33507a);
    outline-offset: 3px;
  }
</style>
