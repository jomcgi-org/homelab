<script>
  import { Button } from "@homelab/design-system/components";
  import { onMount } from "svelte";
  import FactorySheet from "../../FactorySheet.svelte";
  import TrackingItem from "../../TrackingItem.svelte";
  import {
    relativeTime,
    edgeRows,
    eventLines,
    parseOther,
    directionLabel,
  } from "./work-item-view.js";

  let { data } = $props();

  const POLL_MS = 20000;
  const EDGE_DIRECTIONS = [
    ["blocks", "out"],
    ["blocks", "in"],
    ["parent", "out"],
    ["parent", "in"],
    ["supersedes", "out"],
  ];

  // svelte-ignore state_referenced_locally
  let document = $state(data.document ?? null);
  const missing = $derived(data.missing ?? false);
  // svelte-ignore state_referenced_locally
  let unavailable = $state(data.error ?? false);
  let busy = $state(null);
  let failure = $state(null);
  let notice = $state(null);
  let now = $state(Date.now());
  let expandedEdge = $state(null);
  let addingEdge = $state(null);
  let addFormError = $state(null);

  const edges = $derived(document ? edgeRows(document) : {});
  const eventHistory = $derived(document ? eventLines(document, now) : []);

  async function refresh() {
    try {
      const response = await fetch(`/factory/work-items/${data.itemId}`);
      if (!response.ok) throw new Error("unavailable");
      document = await response.json();
      unavailable = false;
      now = Date.now();
    } catch {
      unavailable = true;
    }
  }

  async function addEdge(kind, direction, other, reason) {
    const itemId = document?.item?.id;
    if (!itemId || busy) return;

    busy = "add";
    failure = null;
    notice = null;
    addFormError = null;

    try {
      const response = await fetch(`/factory/work-items/${itemId}/edges`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          kind,
          direction,
          other,
          stated_reason: reason || null,
        }),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        failure = result.detail ?? `add failed (${response.status})`;
        return;
      }
      document = result;
      addingEdge = null;
      await refresh();
      return true;
    } catch {
      failure = "could not add edge";
      return false;
    } finally {
      busy = null;
    }
  }

  async function deleteEdge(edgeId) {
    const itemId = document?.item?.id;
    if (!itemId || busy) return;

    busy = "delete";
    failure = null;
    notice = null;

    try {
      const response = await fetch(
        `/factory/work-items/${itemId}/edges/${edgeId}`,
        { method: "DELETE" },
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok) {
        failure = result.detail ?? `delete failed (${response.status})`;
        return;
      }
      document = result;
      expandedEdge = null;
      await refresh();
    } catch {
      failure = "could not delete edge";
    } finally {
      busy = null;
    }
  }

  onMount(() => {
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && !busy) refresh();
    }, POLL_MS);
    const tick = setInterval(() => (now = Date.now()), 30000);
    return () => {
      clearInterval(timer);
      clearInterval(tick);
    };
  });
</script>

<svelte:head>
  <title>Work Item</title>
</svelte:head>

<FactorySheet className="work-item-page" view="work-item">
  <h1 class="sr-only">Work Item</h1>

  {#if missing}
    <p class="none">No work item <code>{data.itemId}</code>.</p>
    <p><a href="/factory">Back to factory</a></p>
  {/if}

  {#if unavailable}
    <p class="warn-line">Work item unavailable. Retrying every 20 seconds.</p>
  {/if}

  {#if failure}
    <p class="warn-line" role="alert">{failure}</p>
  {/if}

  {#if notice}
    <p class="warn-line" role="status">{notice}</p>
  {/if}

  {#if document}
    {#if document.item.execution_mode === "tracking_only"}
      <TrackingItem item={document.item} detail />
    {:else}
    <header class="item-header edition">
      <div class="ed-lead">
        <h2 class="item-id">
          work item <code>{document.item.id}</code>
          {#if document.item.github_issue_number}
            <a
              href={document.item.source_ref}
              class="issue-link code"
              target="_blank"
              rel="noopener noreferrer"
            >
              #{document.item.github_issue_number}
            </a>
          {/if}
        </h2>
        <ul class="item-meta divided">
          <li class="state code">{document.item.state}</li>
          <li class="authority code">{document.item.authority}</li>
          <li class="trust code">{document.item.trust}</li>
          <li class="task-class code">{document.item.task_class}</li>
          {#each document.item.labels as label}
            <li class="label">{label}</li>
          {/each}
        </ul>
        <h3 class="item-title">{document.item.title}</h3>
      </div>
    </header>

    <section class="edition item-section" aria-label="Work item edges">
      <p class="sec-label">/ Blocked by</p>
      {#if !edges.blocked_by?.length}
        <p class="none">none</p>
      {:else}
        {#each edges.blocked_by as edge}
          <div class="edge-line">
            <a href="/factory/work-items/{edge.from_id}" class="edge-link code">
              work item {edge.from_id}
            </a>
            <span class="tag code">{edge.source}</span>
            <Button
              variant="secondary"
              class="remove-btn"
              aria-label="remove edge"
              disabled={busy !== null}
              onclick={() => deleteEdge(edge.id)}
            >
              x
            </Button>
          </div>
        {/each}
      {/if}

      <p class="sec-label">/ Blocks</p>
      {#if !edges.blocks?.length}
        <p class="none">none</p>
      {:else}
        {#each edges.blocks as edge}
          <div class="edge-line">
            <a href="/factory/work-items/{edge.to_id}" class="edge-link code">
              work item {edge.to_id}
            </a>
            <span class="tag code">{edge.source}</span>
            <Button
              variant="secondary"
              class="remove-btn"
              aria-label="remove edge"
              disabled={busy !== null}
              onclick={() => deleteEdge(edge.id)}
            >
              x
            </Button>
          </div>
        {/each}
      {/if}

      <p class="sec-label">/ Parent</p>
      {#if !edges.parent?.length}
        <p class="none">none</p>
      {:else}
        {#each edges.parent as edge}
          <div class="edge-line">
            <a href="/factory/work-items/{edge.from_id}" class="edge-link code">
              work item {edge.from_id}
            </a>
            <span class="tag code">{edge.source}</span>
            <Button
              variant="secondary"
              class="remove-btn"
              aria-label="remove edge"
              disabled={busy !== null}
              onclick={() => deleteEdge(edge.id)}
            >
              x
            </Button>
          </div>
        {/each}
      {/if}

      <p class="sec-label">/ Children</p>
      {#if !edges.children?.length}
        <p class="none">none</p>
      {:else}
        {#each edges.children as edge}
          <div class="edge-line">
            <a href="/factory/work-items/{edge.to_id}" class="edge-link code">
              work item {edge.to_id}
            </a>
            <span class="tag code">{edge.source}</span>
            <Button
              variant="secondary"
              class="remove-btn"
              aria-label="remove edge"
              disabled={busy !== null}
              onclick={() => deleteEdge(edge.id)}
            >
              x
            </Button>
          </div>
        {/each}
      {/if}

      <p class="sec-label">/ Supersedes</p>
      {#if !edges.supersedes?.length}
        <p class="none">none</p>
      {:else}
        {#each edges.supersedes as edge}
          <div class="edge-line">
            <a href="/factory/work-items/{edge.to_id}" class="edge-link code">
              work item {edge.to_id}
            </a>
            <span class="tag code">{edge.source}</span>
            <Button
              variant="secondary"
              class="remove-btn"
              aria-label="remove edge"
              disabled={busy !== null}
              onclick={() => deleteEdge(edge.id)}
            >
              x
            </Button>
          </div>
        {/each}
      {/if}

      <p class="sec-label">/ Superseded by</p>
      {#if !edges.superseded_by?.length}
        <p class="none">none</p>
      {:else}
        {#each edges.superseded_by as edge}
          <div class="edge-line">
            <a href="/factory/work-items/{edge.from_id}" class="edge-link code">
              work item {edge.from_id}
            </a>
            <span class="tag code">{edge.source}</span>
            <Button
              variant="secondary"
              class="remove-btn"
              aria-label="remove edge"
              disabled={busy !== null}
              onclick={() => deleteEdge(edge.id)}
            >
              x
            </Button>
          </div>
        {/each}
      {/if}
    </section>

    <form
      class="add-form"
      onsubmit={async (e) => {
        e.preventDefault();
        const form = e.target;
        const [kind, direction] = form.direction.value.split("|");
        const other = form.other.value.trim();
        const parsed = parseOther(other);
        if (!parsed) {
          addFormError = "Enter work item id or #issue";
          return;
        }
        addFormError = null;
        const otherValue =
          parsed.type === "issue" ? `#${parsed.number}` : parsed.id;
        if (await addEdge(kind, direction, otherValue)) {
          form.other.value = "";
        }
      }}
    >
      <label>
        <span class="label-text">Kind and direction</span>
        <select name="direction" disabled={busy !== null}>
          {#each EDGE_DIRECTIONS as [kind, direction]}
            <option value={`${kind}|${direction}`}>
              {directionLabel(kind, direction)}
            </option>
          {/each}
        </select>
      </label>
      <label>
        <span class="label-text">Work item id or #issue</span>
        <input
          name="other"
          type="text"
          placeholder="work item id or #issue"
          disabled={busy !== null}
          onkeydown={(e) => {
            if (e.key === "Escape") {
              e.currentTarget.value = "";
            }
          }}
        />
      </label>
      <Button
        type="submit"
        variant="secondary"
        class="submit-btn"
        disabled={busy !== null}
      >
        add
      </Button>
    </form>
    {#if addFormError}
      <p class="warn-line" role="alert">{addFormError}</p>
    {/if}

    {#if document.receipts?.length}
      <section class="edition item-section" aria-label="Receipts">
        <p class="sec-label">/ Receipts</p>
        {#each document.receipts as receipt}
          {#if receipt.state === "escalated"}
            <p class="receipt-line">
              <a href="/factory/escalations">
                receipt <code class="code">{receipt.id}</code> gen
                <code class="code">{receipt.generation}</code>
                <span>{receipt.task_class}</span>
                <span class="state code">{receipt.state}</span>
                <span
                  >{relativeTime(
                    new Date(receipt.created_at).getTime(),
                    now,
                  )}</span
                >
              </a>
            </p>
          {:else}
            <p class="receipt-line">
              receipt <code class="code">{receipt.id}</code> gen
              <code class="code">{receipt.generation}</code>
              <span>{receipt.task_class}</span>
              <span class="state code">{receipt.state}</span>
              <span
                >{relativeTime(
                  new Date(receipt.created_at).getTime(),
                  now,
                )}</span
              >
            </p>
          {/if}
        {/each}
      </section>
    {/if}

    {/if}
    <section class="edition item-section" aria-label="Event history">
      <p class="sec-label">/ Events</p>
      {#if !eventHistory.length}
        <p class="none">none</p>
      {:else}
        {#each eventHistory as event}
          {#if event.change_json}
            <details>
              <summary class="event-summary code">
                {event.op}
                <span class="event-meta">
                  by {event.author_kind}
                  {event.author}
                  {event.relativeTime}
                </span>
              </summary>
              <pre class="event-json">{JSON.stringify(
                  JSON.parse(event.change_json),
                  null,
                  2,
                )}</pre>
            </details>
          {:else}
            <p class="event-line code">
              {event.op}
              <span class="event-meta">
                by {event.author_kind}
                {event.author}
                {event.relativeTime}
              </span>
            </p>
          {/if}
        {/each}
      {/if}
    </section>
  {/if}
</FactorySheet>

<style>
  .code {
    font-family: var(--font-code);
  }
  a {
    color: var(--accent-ink);
  }
  .warn-line {
    margin: 0.75rem 0 0;
    color: var(--warn);
    font-family: var(--font-code);
    font-size: 0.75rem;
  }

  .none {
    margin: 0;
    color: var(--ink-2);
    font-size: 0.9rem;
  }

  .item-header {
    margin-bottom: 2rem;
  }
  .item-id {
    margin: 0 0 0.6em;
    font-size: 1.1rem;
    font-weight: 600;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
  }
  .item-id code {
    font-family: var(--font-code);
  }
  .issue-link {
    color: var(--accent-ink);
    text-decoration: none;
  }
  .issue-link:hover {
    text-decoration: underline;
  }
  .item-meta {
    margin-bottom: 0.8em;
    border-block: 1px solid var(--stroke);
  }
  .item-title {
    margin: 0;
    font-size: 1.4rem;
    font-weight: 700;
  }

  section {
    margin-top: 2.25rem;
  }
  .item-section {
    padding: 1rem;
  }
  .edge-line + .sec-label {
    margin-top: 1.5rem;
  }

  .edge-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.8rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--stroke);
    font-size: 0.9rem;
  }
  .edge-link {
    color: var(--accent-ink);
    text-decoration: none;
  }
  .edge-link:hover {
    text-decoration: underline;
  }
  .tag {
    color: var(--ink-2);
    font-size: 0.75rem;
  }
  .edge-line :global(.remove-btn) {
    margin-left: auto;
  }

  .add-form {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto;
    gap: 0.5rem;
    align-items: end;
    margin-top: 2.25rem;
    padding: 0.8rem;
    border: 1px solid var(--stroke);
    background: var(--band);
  }
  .add-form label {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .label-text {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
  }
  .add-form input,
  .add-form select {
    padding: 0.5rem 0.6rem;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 0.9rem;
  }
  .add-form input:focus,
  .add-form select:focus {
    background: var(--band);
  }
  .add-form input:disabled,
  .add-form select:disabled {
    opacity: 0.55;
  }
  .receipt-line {
    margin: 0 0 0.45rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--stroke);
    font-size: 0.9rem;
  }
  .receipt-line a {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.4rem 0.8rem;
    text-decoration: none;
  }
  .receipt-line a:hover {
    text-decoration: underline;
  }
  .receipt-line code {
    font-family: var(--font-code);
  }
  .receipt-line .state {
    color: var(--ink-2);
    font-size: 0.75rem;
  }

  .event-summary {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.8rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--stroke);
    cursor: pointer;
    font-size: 0.9rem;
  }
  .event-summary:hover {
    color: var(--accent-ink);
  }
  .event-meta {
    margin-left: auto;
    color: var(--ink-2);
    font-size: 0.75rem;
  }
  .event-line {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.8rem;
    padding: 0.45rem 0;
    border-bottom: 1px solid var(--stroke);
    font-size: 0.9rem;
  }
  .event-json {
    margin: 0.6rem 0;
    padding: 0.6rem;
    border: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.75rem;
    overflow-x: auto;
  }

  @media (max-width: 40em) {
    .add-form {
      grid-template-columns: minmax(0, 1fr);
    }
  }
</style>
