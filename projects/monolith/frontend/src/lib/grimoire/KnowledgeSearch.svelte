<script>
  import { onDestroy } from "svelte";
  import { chunkHref } from "$lib/public/grimoire/api.js";

  let { campaignId } = $props();
  const inputId = $props.id();
  const badges = {
    entity: "Entity",
    note: "Note",
    event: "Event",
    chunk: "Lore",
  };
  let query = $state("");
  let results = $state([]);
  let searched = $state(false);
  let loading = $state(false);
  let failure = $state("");
  let request;
  // Clear private results on campaign navigation and abort an older request.
  $effect(() => {
    campaignId;
    request?.abort();
    results = [];
    query = "";
    searched = false;
    loading = false;
    failure = "";
  });
  onDestroy(() => request?.abort());

  function href(result) {
    const base = `/grimoire/campaigns/${encodeURIComponent(campaignId)}`;
    const source = result.source;
    if (result.type === "entity")
      return `${base}/entities/${encodeURIComponent(source.entity_id)}`;
    if (result.type === "note") {
      const note = encodeURIComponent(source.note_id);
      return `${base}/notes?note=${note}#note-${note}`;
    }
    if (result.type === "event") {
      const session = encodeURIComponent(source.session_id);
      return `${base}/session?session=${session}#session-${session}-event-${encodeURIComponent(source.seq)}`;
    }
    // The reader lives on the public tier; friends-host relative URLs reroute
    // under /friends and have no reader route.
    return `https://jomcgi.dev${chunkHref(source.book_id, source.chunk_id)}`;
  }

  // Entity hits are existing grant projections, with no preview field. Only
  // render their authorized revealed_details or identity, never fetch detail.
  function preview(result) {
    return (
      result.preview ??
      (result.revealed_details
        ? JSON.stringify(result.revealed_details)
        : result.entity_type?.replaceAll("_", " ")) ??
      ""
    );
  }

  async function search(event) {
    event.preventDefault();
    request?.abort();
    const current = new AbortController();
    request = current;
    const q = query.trim();
    results = [];
    searched = false;
    failure = "";
    loading = false;
    if (!q || q.length > 200) {
      failure = "Enter a search of 1 to 200 characters.";
      return;
    }
    loading = true;
    try {
      const response = await fetch(
        `/grimoire/campaigns/${encodeURIComponent(campaignId)}/knowledge/search?${new URLSearchParams({ q })}`,
        { signal: current.signal, cache: "no-store" },
      );
      const body = await response.json();
      if (!response.ok)
        throw new Error(body.error || "Knowledge search is unavailable.");
      if (!Array.isArray(body))
        throw new Error("Knowledge search is unavailable.");
      if (current.signal.aborted) return;
      results = body;
      searched = true;
    } catch (cause) {
      if (!current.signal.aborted)
        failure = cause.message || "Knowledge search is unavailable.";
    } finally {
      if (request === current) loading = false;
    }
  }
</script>

<section class="knowledge-search" aria-label="Character knowledge search">
  <form onsubmit={search} role="search">
    <label for={inputId}>Search what your character knows</label>
    <div class="controls">
      <input
        id={inputId}
        type="search"
        name="q"
        bind:value={query}
        maxlength="200"
        required
      />
      <button type="submit" disabled={loading}>Search knowledge</button>
    </div>
  </form>
  <p role="status" aria-live="polite" aria-atomic="true">
    {#if loading}Searching knowledge…
    {:else if searched}{`${results.length} ${results.length === 1 ? "result" : "results"} found.`}{/if}
  </p>
  {#if failure}<p role="alert">{failure}</p>{/if}
  {#if searched && !results.length}<p>No knowledge matches your search.</p>{/if}
  {#if results.length}
    <ul aria-label="Knowledge results">
      {#each results as result (`${result.type}:${result.id}`)}
        <li>
          <span class="badge">{badges[result.type]}</span>
          <a href={href(result)}
            >{result.name ||
              result.title ||
              result.display_name ||
              `Session event ${result.source.seq}`}</a
          >
          <p>{preview(result)}</p>
        </li>
      {/each}
    </ul>
  {/if}
</section>

<style>
  .knowledge-search {
    margin: 1.5rem 0;
    padding: 1rem;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    color: var(--grim-ink);
    overflow-wrap: anywhere;
  }
  label {
    display: block;
    font-weight: 700;
    margin-bottom: 0.5rem;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  input {
    flex: 1;
    min-width: 0;
  }
  input,
  button {
    font: inherit;
    min-height: 44px;
    padding: 0.55rem 0.75rem;
    border: 1px solid var(--grim-accent);
  }
  input {
    background: var(--grim-paper);
    color: var(--grim-ink);
  }
  button {
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    cursor: pointer;
  }
  button:disabled {
    cursor: wait;
  }
  a {
    color: var(--grim-accent);
    font-weight: 700;
  }
  :is(input, button, a):focus-visible {
    outline: 2px solid var(--grim-accent);
    outline-offset: 3px;
  }
  ul {
    list-style: none;
    padding: 0;
    margin: 0;
  }
  li {
    padding: 0.75rem 0;
    border-top: 1px solid var(--grim-line);
  }
  .badge {
    display: inline-block;
    margin-right: 0.5rem;
    padding: 0.15rem 0.4rem;
    border: 1px solid var(--grim-line);
    background: var(--grim-paper);
    font-size: 0.875rem;
    font-family: var(--font-mono);
  }
  p {
    white-space: pre-wrap;
    margin: 0.5rem 0 0;
  }
</style>
