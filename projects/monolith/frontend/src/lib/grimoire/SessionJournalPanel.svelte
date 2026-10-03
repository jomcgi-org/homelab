<script>
  import Markdown from "$lib/public/factory/Markdown.svelte";
  let { views, showEvent, openKnowledge } = $props();
  let party = $state(false);
  const sections = [
    ["learned", "Learned"],
    ["received", "Received"],
    ["people_places", "People and places"],
    ["rolls", "Rolls"],
    ["open_threads", "Open threads"],
  ];
  let current = $derived(party ? views?.party : views?.mine);
</script>

<section aria-label="Session journal" class="journal">
  <nav aria-label="Journal audience">
    <button aria-pressed={!party} onclick={() => (party = false)}
      >My journal</button
    ><button aria-pressed={party} onclick={() => (party = true)}
      >Party journal</button
    >
  </nav>
  <p>
    {party
      ? "Only events shared with everyone at the table."
      : "What you learned, received and did in this session."}
  </p>
  {#each sections as [key, title]}
    <section aria-label={title}>
      <h2>{title}</h2>
      {#if !current?.[key]?.length}<p class="empty">Nothing here yet.</p>{/if}
      {#each current?.[key] || [] as entry}
        <article>
          {#if entry.retracted}<p>Knowledge retracted: {entry.name}.</p>
          {:else if key === "rolls"}<p>
              <strong>{entry.total}</strong> · {entry.label || "Roll"} · {entry.formula}
            </p>
          {:else}<h3>
              {entry.name ||
                entry.title ||
                entry.label ||
                (key === "open_threads" ? "Waiting for DM" : "Session entry")}
            </h3>
            {#if entry.projection?.recognition_only}<p>
                You recognize this name.
              </p>{/if}
            <Markdown
              text={entry.text ||
                entry.markdown ||
                entry.projection?.revealed_details?.clue ||
                entry.projection?.description ||
                ""}
            />
          {/if}
          <button class="source" onclick={() => showEvent(entry.event_id)}
            >Show in story</button
          >
          {#if entry.entity_id && !entry.retracted && entry.grant_scope !== "name_only"}<button
              class="source"
              onclick={() => openKnowledge(entry.entity_id)}
              >Explore {entry.name || "knowledge"}</button
            >{/if}
        </article>
      {/each}
    </section>
  {/each}
</section>

<style>
  .journal {
    max-width: 760px;
    margin: 28px auto;
  }
  nav {
    display: flex;
    gap: 12px;
  }
  button {
    font: inherit;
    padding: 12px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    color: var(--grim-ink);
    cursor: pointer;
  }
  button[aria-pressed="true"] {
    background: var(--grim-accent);
    color: white;
  }
  section section {
    border-top: 1px solid var(--grim-line);
    margin-top: 24px;
    padding-top: 16px;
  }
  article {
    margin: 16px 0;
    overflow-wrap: anywhere;
  }
  .source {
    padding: 6px 10px;
  }
  .empty {
    color: var(--grim-ink-soft);
  }
</style>
