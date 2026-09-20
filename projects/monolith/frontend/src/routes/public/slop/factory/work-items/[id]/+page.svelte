<script>
  import { SchemeToggle, Seo } from "$lib/public/components";
  import "$lib/public/factory/factory.css";
  import Trail from "../../../Trail.svelte";

  let { data } = $props();
  const document = $derived(data.document);
  const item = $derived(document.item);
  const edges = $derived([
    ...(document.edges_in ?? []).map((edge) => ({
      label: edge.kind === "blocks" ? "blocked by" : `${edge.kind} from`,
      id: edge.from_id,
      source: edge.source,
    })),
    ...(document.edges_out ?? []).map((edge) => ({
      label: edge.kind,
      id: edge.to_id,
      source: edge.source,
    })),
  ]);
</script>

<Seo
  title={`Work item ${item.id} · Factory · jomcgi.dev`}
  description={item.title}
  path={`/slop/factory/work-items/${item.id}`}
/>

<main class="td factory-page work-item-page">
  <div class="frame">
    <header class="masthead">
      <h1 class="sr-only">Factory work item</h1>
      <Trail
        crumbs={[
          { label: "factory", href: "/slop/factory" },
          { label: "work items" },
          { label: String(item.id) },
        ]}
      />
      <div class="mast-actions">
        <nav class="view-tabs" aria-label="Factory views">
          <a href="/slop/factory">overview</a>
          <a href="/slop/factory/activity">activity</a>
          <a href="/slop/factory/context">context</a>
        </nav>
        <SchemeToggle />
      </div>
    </header>

    <article class="work-item-card">
      <div class="identity">
        <span>work item <b>{item.id}</b></span>
        <a href={item.source_ref} rel="noopener noreferrer">
          GitHub #{item.github_issue_number}
        </a>
      </div>
      <h2>{item.title}</h2>
      <div class="chips" aria-label="Work item state">
        <span>{item.state}</span>
        <span>{item.authority}</span>
        <span>{item.trust}</span>
        {#if item.task_class}<span>{item.task_class}</span>{/if}
        {#each item.labels ?? [] as label}<span>{label}</span>{/each}
      </div>

      <section aria-label="Work item relationships">
        <p class="sec-label">/ Relationships</p>
        {#if edges.length}
          <ul class="edge-list">
            {#each edges as edge}
              <li>
                <span>{edge.label}</span>
                <a href={`/slop/factory/work-items/${edge.id}`}>work item {edge.id}</a>
                <small>{edge.source}</small>
              </li>
            {/each}
          </ul>
        {:else}
          <p class="none">none</p>
        {/if}
      </section>
    </article>
  </div>
</main>

<style>
  .work-item-page .frame {
    max-width: 58em;
  }
  .work-item-card {
    padding: clamp(1rem, 3vw, 2rem);
    border: 1px solid var(--ink);
  }
  .identity {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.78rem;
  }
  h2 {
    margin: 0.7rem 0 1rem;
    font-size: clamp(1.55rem, 4vw, 2.5rem);
    line-height: 1.08;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
    margin-bottom: 2rem;
  }
  .chips span {
    padding: 0.18rem 0.45rem;
    border: 1px solid var(--stroke);
    font-family: var(--font-code);
    font-size: 0.68rem;
  }
  .edge-list {
    padding: 0;
    margin: 0;
    list-style: none;
  }
  .edge-list li {
    display: grid;
    grid-template-columns: 8em 1fr auto;
    gap: 0.7rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--stroke);
  }
  .edge-list small,
  .none {
    color: var(--ink-2);
    font-family: var(--font-code);
  }
  @media (max-width: 680px) {
    .factory-page {
      padding-inline: 1rem;
    }
    .identity,
    .edge-list li {
      display: flex;
      flex-direction: column;
    }
  }
</style>
