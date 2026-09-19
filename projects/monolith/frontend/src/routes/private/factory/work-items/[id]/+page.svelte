<script>
  import { onMount } from "svelte";
  import "$lib/private/dashboard-theme.css";
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

  let dark = $state(
    typeof window !== "undefined" &&
      window.matchMedia("(prefers-color-scheme: dark)").matches,
  );

  // svelte-ignore state_referenced_locally
  let document = $state(data.document ?? null);
  const missing = $derived(data.missing ?? false);
  const unavailable = $derived(data.error ?? false);
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
      now = Date.now();
    } catch {
      // Polling errors don't change the displayed state, just keep retrying
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
    const scheme = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => (dark = scheme.matches);
    apply();
    scheme.addEventListener("change", apply);
    const timer = setInterval(() => {
      if (typeof document !== "undefined" && !busy) refresh();
    }, POLL_MS);
    const tick = setInterval(() => (now = Date.now()), 30000);
    return () => {
      scheme.removeEventListener("change", apply);
      clearInterval(timer);
      clearInterval(tick);
    };
  });
</script>

<svelte:head>
  <title>Work Item</title>
</svelte:head>

<main class="work-item-page shell {dark ? 'night' : 'day'}">
  <div class="frame">
    <h1 class="sr-only">Work Item</h1>

    <header class="masthead">
      <nav class="view-tabs" aria-label="Factory views">
        <a href="/factory">factory</a>
        <a class="here" href="/factory/work-items" aria-current="page"
          >work items</a
        >
        <a href="/factory/escalations">escalations</a>
        <a href="/factory/execution">work</a>
      </nav>
    </header>

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
      <header class="item-header">
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
        <div class="item-meta">
          <span class="chip state code">{document.item.state}</span>
          <span class="chip authority code">{document.item.authority}</span>
          <span class="chip trust code">{document.item.trust}</span>
          <span class="chip task-class code">{document.item.task_class}</span>
          {#each document.item.labels as label}
            <span class="chip label">{label}</span>
          {/each}
        </div>
        <h3 class="item-title">{document.item.title}</h3>
      </header>

      <section aria-label="Work item edges">
        <p class="sec-label">/ Blocked by</p>
        {#if !edges.blocked_by?.length}
          <p class="none">none</p>
        {:else}
          {#each edges.blocked_by as edge}
            <div class="edge-line">
              <a
                href="/factory/work-items/{edge.from_id}"
                class="edge-link code"
              >
                work item {edge.from_id}
              </a>
              <span class="tag code">{edge.source}</span>
              <button
                class="remove-btn"
                aria-label="remove edge"
                disabled={busy !== null}
                onclick={() => deleteEdge(edge.id)}
              >
                x
              </button>
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
              <button
                class="remove-btn"
                aria-label="remove edge"
                disabled={busy !== null}
                onclick={() => deleteEdge(edge.id)}
              >
                x
              </button>
            </div>
          {/each}
        {/if}

        <p class="sec-label">/ Parent</p>
        {#if !edges.parent?.length}
          <p class="none">none</p>
        {:else}
          {#each edges.parent as edge}
            <div class="edge-line">
              <a
                href="/factory/work-items/{edge.from_id}"
                class="edge-link code"
              >
                work item {edge.from_id}
              </a>
              <span class="tag code">{edge.source}</span>
              <button
                class="remove-btn"
                aria-label="remove edge"
                disabled={busy !== null}
                onclick={() => deleteEdge(edge.id)}
              >
                x
              </button>
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
              <button
                class="remove-btn"
                aria-label="remove edge"
                disabled={busy !== null}
                onclick={() => deleteEdge(edge.id)}
              >
                x
              </button>
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
              <button
                class="remove-btn"
                aria-label="remove edge"
                disabled={busy !== null}
                onclick={() => deleteEdge(edge.id)}
              >
                x
              </button>
            </div>
          {/each}
        {/if}

        <p class="sec-label">/ Superseded by</p>
        {#if !edges.superseded_by?.length}
          <p class="none">none</p>
        {:else}
          {#each edges.superseded_by as edge}
            <div class="edge-line">
              <a
                href="/factory/work-items/{edge.from_id}"
                class="edge-link code"
              >
                work item {edge.from_id}
              </a>
              <span class="tag code">{edge.source}</span>
              <button
                class="remove-btn"
                aria-label="remove edge"
                disabled={busy !== null}
                onclick={() => deleteEdge(edge.id)}
              >
                x
              </button>
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
        <button type="submit" class="submit-btn" disabled={busy !== null}>
          add
        </button>
      </form>
      {#if addFormError}
        <p class="warn-line" role="alert">{addFormError}</p>
      {/if}

      {#if document.receipts?.length}
        <section aria-label="Receipts">
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

      <section aria-label="Event history">
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
  </div>
</main>

<style>
  :global(body) {
    margin: 0;
  }

  :global(html:has(.work-item-page)) {
    background: #ffffff; /* nosemgrep: svelte-hardcoded-color-in-style */
    font-size: 16px;
  }
  @media (prefers-color-scheme: dark) {
    :global(html:has(.work-item-page)) {
      background: #181a20; /* nosemgrep: svelte-hardcoded-color-in-style */
    }
  }

  .work-item-page {
    --band: color-mix(in srgb, var(--ink) 5%, var(--sheet));
    --accent-ink: color-mix(in srgb, var(--accent) 85%, var(--ink));
    --stroke: color-mix(in srgb, var(--ink) 28%, transparent);
    min-height: 100vh;
    box-sizing: border-box;
    padding: clamp(3.25rem, 5vh, 3.75rem) clamp(1rem, 4vw, 4.5em) 6em;
    color: var(--ink);
    background: var(--sheet);
    font-family: var(--font-ui);
    font-size: 16px;
    line-height: 1.45;
  }
  .frame {
    max-width: 56em;
    margin: 0 auto;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    overflow: hidden;
    clip-path: inset(50%);
    white-space: nowrap;
  }
  .code {
    font-family: var(--font-code);
  }
  a {
    color: var(--accent-ink);
  }
  a:focus-visible,
  button:focus-visible,
  input:focus-visible,
  select:focus-visible,
  .masthead {
    display: flex;
    flex-wrap: wrap;
    gap: 0.75rem;
    margin-bottom: 1rem;
  }
  .view-tabs {
    display: flex;
    flex-wrap: wrap;
    border: 1px solid var(--stroke);
    font-family: var(--font-code);
    font-size: 0.72rem;
    letter-spacing: 0.04em;
  }
  .view-tabs > a {
    display: inline-flex;
    align-items: center;
    min-height: 2.75rem;
    padding: 0 0.9em;
    border-bottom: 2px solid transparent;
    color: var(--ink-2);
    text-decoration: none;
  }
  .view-tabs > a + a {
    border-left: 1px solid var(--stroke);
  }
  .view-tabs a:hover {
    color: var(--ink);
  }
  .view-tabs .here {
    border-bottom-color: var(--accent-ink);
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
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem 0.6rem;
    margin-bottom: 0.8em;
  }
  .chip {
    padding: 0.1em 0.55em;
    border: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.06em;
  }
  .item-title {
    margin: 0;
    font-size: 1.4rem;
    font-weight: 700;
  }

  section {
    margin-top: 2.25rem;
  }
  .sec-label {
    margin: 0 0 0.8em;
    padding-bottom: 0.5em;
    border-bottom: 1px solid var(--stroke);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
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
  .remove-btn {
    margin-left: auto;
    padding: 0.2em 0.4em;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.75rem;
    cursor: pointer;
    text-decoration: none;
  }
  .remove-btn:hover:not(:disabled) {
    background: var(--band);
    border-color: var(--ink);
  }
  .remove-btn:disabled {
    opacity: 0.55;
    cursor: default;
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
  .submit-btn {
    padding: 0.5rem 1rem;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.9rem;
    cursor: pointer;
  }
  .submit-btn:hover:not(:disabled) {
    background: var(--band);
    border-color: var(--ink);
  }
  .submit-btn:disabled {
    opacity: 0.55;
    cursor: default;
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
      grid-template-columns: 1fr;
    }
    .edge-line {
      flex-direction: column;
      align-items: flex-start;
    }
    .remove-btn {
      margin-left: 0;
      margin-top: 0.3rem;
    }
  }
</style>
