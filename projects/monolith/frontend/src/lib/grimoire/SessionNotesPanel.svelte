<script>
  import { onMount } from "svelte";
  import Markdown from "$lib/public/factory/Markdown.svelte";
  let { endpoint, dm = false, showEvent, openKnowledge } = $props();
  let kind = $state("character");
  let notes = $state([]);
  let noteId = $state(null);
  let title = $state("");
  let markdown = $state("");
  let dmReadable = $state(false);
  let pinned = $state(false);
  let busy = $state(false);
  let failure = $state("");
  let notice = $state("");

  async function load() {
    const requestedKind = kind;
    try {
      const response = await fetch(`${endpoint}?notes=${requestedKind}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (kind === requestedKind) notes = result;
    } catch (error) {
      failure = error.message;
    }
  }
  function clear() {
    noteId = null;
    title = "";
    markdown = "";
    dmReadable = false;
    pinned = false;
  }
  async function tab(next) {
    kind = next;
    clear();
    notice = "";
    await load();
  }
  function edit(note) {
    noteId = note.id;
    title = note.title;
    markdown = note.markdown;
    dmReadable = note.dm_readable;
    pinned = note.pinned;
  }
  async function save(event) {
    event.preventDefault();
    busy = true;
    failure = "";
    notice = "";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          operation: "note",
          noteId,
          kind,
          title,
          markdown,
          dmReadable,
          pinned,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      clear();
      notice = "Note saved.";
      await load();
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }
  async function remove(id) {
    busy = true;
    failure = "";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ operation: "deleteNote", noteId: id }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      if (noteId === id) clear();
      notice = "Note deleted.";
      await load();
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }
  onMount(() => {
    load();
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  });
</script>

<section class="notes" aria-label="Campaign notes">
  <nav aria-label="Notes audience">
    <button
      disabled={busy}
      aria-pressed={kind === "character"}
      onclick={() => tab("character")}
      >{dm ? "Shared with you" : "My notes"}</button
    ><button
      disabled={busy}
      aria-pressed={kind === "party"}
      onclick={() => tab("party")}>Party notes</button
    >
  </nav>
  <p>
    {kind === "party"
      ? "Shared with the DM and every player with a character."
      : dm
        ? "Character notes players explicitly shared with you."
        : "Only you can read these unless you share a note with the DM."}
  </p>
  {#if !dm || kind === "party"}<form onsubmit={save}>
      <h2>{noteId ? "Edit note" : "Add a note"}</h2>
      <label
        >Note title<input
          disabled={busy}
          bind:value={title}
          maxlength="200"
          required
        /></label
      >
      <label
        >Note text<textarea
          disabled={busy}
          bind:value={markdown}
          maxlength="20000"
          rows="5"
          required></textarea></label
      >
      <small>Markdown works here: headings, lists, links and emphasis.</small>
      {#if kind === "character" && !dm}<label class="check"
          ><input
            disabled={busy}
            type="checkbox"
            bind:checked={dmReadable}
          />Share this note with the DM</label
        >{/if}
      <label class="check"
        ><input disabled={busy} type="checkbox" bind:checked={pinned} />Keep
        pinned</label
      >
      <div class="actions">
        <button disabled={busy}>Save note</button>{#if noteId}<button
            type="button"
            disabled={busy}
            onclick={clear}>Cancel edit</button
          >{/if}
      </div>
    </form>{/if}
  {#if failure}<p role="alert">{failure}</p>{/if}
  {#if notice}<p role="status">{notice}</p>{/if}
  {#if !notes.length}<p>No notes here yet.</p>{/if}
  {#each notes as note (note.id)}
    <article>
      <h3>{note.title}{note.pinned ? " · Pinned" : ""}</h3>
      <Markdown text={note.markdown} />
      {#if note.kind === "character" && note.dm_readable}<small
          >Shared with the DM</small
        >{/if}
      {#each note.links?.event_ids || [] as id}<button
          class="chip"
          onclick={() => showEvent(id)}>Source event</button
        >{/each}
      {#each note.links?.entity_ids || [] as id}<button
          class="chip"
          onclick={() => openKnowledge(id)}
          >{note.links?.entity_names?.[id] || "Related knowledge"}</button
        >{/each}
      {#if note.editable}<div class="actions">
          <button onclick={() => edit(note)} disabled={busy}
            >Edit {note.title}</button
          ><button onclick={() => remove(note.id)} disabled={busy}
            >Delete {note.title}</button
          >
        </div>{/if}
    </article>
  {/each}
</section>

<style>
  .notes {
    max-width: 760px;
    margin: 28px auto;
  }
  nav,
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }
  form,
  label {
    display: grid;
    gap: 8px;
  }
  input,
  textarea,
  button {
    font: inherit;
    color: var(--grim-ink);
    background: var(--grim-surface);
    padding: 12px;
    border: 1px solid var(--grim-line);
    box-sizing: border-box;
  }
  input,
  textarea {
    width: 100%;
  }
  button {
    cursor: pointer;
  }
  button[aria-pressed="true"] {
    background: var(--grim-accent);
    color: white;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .check input {
    width: auto;
  }
  article {
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid var(--grim-line);
    overflow-wrap: anywhere;
  }
  article .actions {
    margin-top: 12px;
  }
  small {
    color: var(--grim-ink-soft);
  }
  .chip {
    display: inline-block;
    padding: 6px 10px;
    margin: 8px;
    border: 1px solid var(--grim-line);
  }
</style>
