<script>
  import "$lib/grimoire/theme.css";
  import JournalPanel from "$lib/grimoire/JournalPanel.svelte";
  import KnowledgeSearch from "$lib/grimoire/KnowledgeSearch.svelte";

  let { data } = $props();
  const nextPage = $derived(
    data.journal.next_cursor
      ? `?${new URLSearchParams({ view: data.view, cursor: data.journal.next_cursor })}`
      : null,
  );
</script>

<svelte:head><title>{data.campaign.name} journal · Grimoire</title></svelte:head
>

<main class="grimoire">
  <a href="/grimoire">Back to campaigns</a>
  <h1>{data.campaign.name} journal</h1>
  <KnowledgeSearch campaignId={data.campaign.id} />
  <form method="GET" aria-label="Journal audience">
    <button name="view" value="mine" aria-pressed={data.view === "mine"}
      >Mine</button
    >
    <button name="view" value="party" aria-pressed={data.view === "party"}
      >Party</button
    >
  </form>
  {#if !data.journal.sessions.length}<p>No sessions recorded yet.</p>{/if}
  {#each data.journal.sessions as session (session.session_id)}
    <section aria-label={`Session ${session.started_at}`}>
      <h2>
        Session <time datetime={session.started_at}>{session.started_at}</time>
      </h2>
      <JournalPanel
        journal={session.journal}
        view={data.view}
        showViewToggle={false}
      />
    </section>
  {/each}
  {#if nextPage}<a href={nextPage}>Older sessions</a>{/if}
</main>

<style>
  main {
    max-width: 900px;
    margin: 0 auto;
    padding: 2rem 1rem;
    overflow-wrap: anywhere;
  }
  h1,
  h2 {
    font-family: var(--grim-serif);
    margin: 1.5rem 0;
  }
  h2 {
    font-size: 1.25rem;
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
    border-bottom: 3px solid var(--grim-accent);
  }
  :is(button, a):focus-visible {
    outline: 2px solid var(--grim-accent);
    outline-offset: 3px;
  }
  main > section {
    padding-bottom: 1.5rem;
  }
</style>
