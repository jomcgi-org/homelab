<script>
  let {
    journal,
    view = "mine",
    onViewChange,
    showViewToggle = true,
  } = $props();
  const panelId = $props.id();
  const partial = $derived(journal.truncated === true);
  // A truncated journal only folds the earliest events, so an empty section
  // is "nothing yet in the part shown", never a claim about the whole session.
  const emptyText = (text) =>
    partial ? `${text.replace(/\.$/, "")} in the events shown.` : text;
  const eventSections = $derived([
    {
      title: "Received",
      entries: journal.received,
      empty: emptyText("No handouts received yet."),
    },
    {
      title: "Rolls",
      entries: journal.rolls,
      empty: emptyText("No rolls recorded yet."),
    },
    {
      title: "Open threads",
      entries: journal.open_threads,
      empty: emptyText("No open threads."),
    },
  ]);
</script>

{#snippet fields(value)}
  <dl>
    {#each Object.entries(value) as [key, detail] (key)}
      <div>
        <dt>{key.replaceAll("_", " ")}</dt>
        <dd>
          {#if typeof detail === "object" && detail !== null}
            <pre>{JSON.stringify(detail, null, 2)}</pre>
          {:else}{String(detail ?? "")}{/if}
        </dd>
      </div>
    {/each}
  </dl>
{/snippet}

<section class="journal" aria-label="Session journal">
  {#if showViewToggle}
    <form method="GET" aria-label="Journal audience">
      {#each ["mine", "party"] as audience}
        <button
          type="submit"
          name="view"
          value={audience}
          aria-pressed={view === audience}
          onclick={(event) => {
            if (onViewChange) {
              event.preventDefault();
              onViewChange(audience);
            }
          }}>{audience === "mine" ? "Mine" : "Party"}</button
        >
      {/each}
    </form>
  {/if}

  {#if partial}
    <p class="partial" role="status">
      This journal is incomplete: the session has more events than can be shown,
      so later handouts, rolls and replies are missing. Open threads may already
      have been answered.
    </p>
  {/if}

  <section aria-labelledby={`${panelId}-learned`}>
    <h3 id={`${panelId}-learned`}>Learned</h3>
    {#if !journal.learned.length}<p>{emptyText("No discoveries yet.")}</p>{/if}
    {#each journal.learned as entry (`${entry.player_character_id ?? ""}:${entry.entity_id}`)}
      <article>
        {#if entry.name}<h4>{entry.name}</h4>{/if}
        {#if entry.entity_type}<p>{entry.entity_type}</p>{/if}
        {#if entry.retracted}
          <p class="retracted">Retracted</p>
        {:else}
          {#if entry.grant_scope}<p>Scope: {entry.grant_scope}</p>{/if}
          {#if entry.player_character_id}<p>
              Character: {entry.player_character_id}
            </p>{/if}
          {#if entry.entity}{@render fields(entry.entity)}{/if}
        {/if}
      </article>
    {/each}
  </section>

  <section aria-labelledby={`${panelId}-received`}>
    <h3 id={`${panelId}-received`}>{eventSections[0].title}</h3>
    {#if !eventSections[0].entries.length}<p>{eventSections[0].empty}</p>{/if}
    {#each eventSections[0].entries as entry (entry.id)}
      <article>{@render fields(entry.body)}</article>
    {/each}
  </section>

  <section aria-labelledby={`${panelId}-people`}>
    <h3 id={`${panelId}-people`}>People and places</h3>
    {#if !journal.people_and_places.length}<p>
        {emptyText("No people or places recorded yet.")}
      </p>{/if}
    <ul>
      {#each journal.people_and_places as entity (entity.id)}
        <li>
          {entity.name}{#if entity.entity_type}
            <span>({entity.entity_type})</span>{/if}
        </li>
      {/each}
    </ul>
  </section>

  {#each eventSections.slice(1) as section (section.title)}
    <section
      aria-labelledby={`${panelId}-${section.title.replaceAll(" ", "-")}`}
    >
      <h3 id={`${panelId}-${section.title.replaceAll(" ", "-")}`}>
        {section.title}
      </h3>
      {#if !section.entries.length}<p>{section.empty}</p>{/if}
      {#each section.entries as entry (entry.id)}
        <article>{@render fields(entry.body)}</article>
      {/each}
    </section>
  {/each}
</section>

<style>
  .journal {
    display: grid;
    gap: 1.5rem;
    overflow-wrap: anywhere;
  }
  form {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
  }
  button {
    font: inherit;
    min-height: 44px;
    padding: 0.55rem 0.85rem;
    cursor: pointer;
  }
  [aria-pressed="true"] {
    font-weight: 700;
    border-bottom: 3px solid var(--grim-accent, #33507a);
  }
  button:focus-visible {
    outline: 2px solid var(--grim-accent, #33507a);
    outline-offset: 3px;
  }
  h3,
  h4 {
    font-family: var(--grim-serif, Georgia, serif);
    margin: 0 0 0.75rem;
  }
  article {
    margin-top: 0.75rem;
    padding: 1rem;
    border: 1px solid var(--grim-line, #dbe0e7);
    background: var(--grim-paper, #f3f5f7);
    color: var(--grim-ink, #1a1f28);
  }
  p {
    margin: 0.5rem 0;
  }
  .retracted {
    font-weight: 700;
  }
  .partial {
    margin: 0;
    padding: 0.75rem 1rem;
    border: 2px solid var(--grim-accent, #33507a);
    font-weight: 700;
  }
  dl,
  dd {
    margin: 0;
  }
  dl > div + div {
    margin-top: 0.75rem;
  }
  dt {
    font-weight: 700;
  }
  pre {
    font: inherit;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    margin: 0;
  }
  ul {
    padding-left: 1.25rem;
  }
</style>
