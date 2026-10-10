<script>
  import { onMount } from "svelte";

  let {
    endpoint,
    dm = false,
    characters = [],
    prefill = null,
    openKnowledge = () => {},
  } = $props();
  let items = $state([]);
  let drafts = $state({});
  let history = $state({});
  let historyOpen = $state({});
  let busy = $state(false);
  let failure = $state("");
  let loading = $state(true);
  const emptyGive = () => ({
    owner: "party",
    name: "",
    quantity: 1,
    notes: "",
    hidden_from_party: false,
    reason: "",
    entity_id: null,
    source_event_id: null,
  });
  let give = $state(emptyGive());
  let sections = $derived(
    dm
      ? [
          { owner: "party", name: "Party pool" },
          ...characters.map((pc) => ({
            owner: pc.id,
            name: pc.character_name,
          })),
        ]
      : [
          { owner: "party", name: "Party pool" },
          { owner: "mine", name: "Your inventory" },
        ],
  );

  $effect(() => {
    if (prefill) give = { ...emptyGive(), ...prefill };
  });

  async function read(query) {
    const response = await fetch(`${endpoint}?${query}`);
    const result = await response.json();
    if (!response.ok)
      throw new Error(result.error || "Could not load inventory.");
    return result;
  }

  async function load(resetId = null) {
    try {
      const nextItems = await read("inventory=items");
      const nextDrafts = {};
      for (const item of nextItems) {
        nextDrafts[item.id] = (item.id !== resetId && drafts[item.id]) || {
          name: item.name,
          notes: item.notes,
          quantity: item.quantity,
          hidden_from_party: item.hidden_from_party,
          reason: "",
          owner:
            item.owner === "party" ? characters[0]?.id || "party" : "party",
          moveQuantity: item.quantity,
          moveReason: "",
        };
      }
      drafts = nextDrafts;
      items = nextItems;
    } catch (error) {
      failure = error.message;
    } finally {
      loading = false;
    }
  }

  async function loadHistory(id) {
    try {
      history[id] = await read(
        `inventory=changes&item=${encodeURIComponent(id)}`,
      );
    } catch (error) {
      failure = error.message;
    }
  }

  async function mutate(input) {
    if (busy) return;
    busy = true;
    failure = "";
    try {
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(input),
      });
      const result = await response.json();
      if (!response.ok)
        throw new Error(
          result.error || "Could not save. Your input is still here.",
        );
      if (input.operation === "giveItem") give = emptyGive();
      await load(input.itemId);
      await Promise.all(
        items
          .filter((item) => historyOpen[item.id])
          .map((item) => loadHistory(item.id)),
      );
    } catch (error) {
      failure = error.message;
    } finally {
      busy = false;
    }
  }

  function update(event, item) {
    event.preventDefault();
    const draft = drafts[item.id];
    mutate({
      operation: "updateItem",
      itemId: item.id,
      quantity: draft.quantity,
      reason: draft.reason,
      ...(dm
        ? {
            name: draft.name,
            notes: draft.notes,
            hidden_from_party: draft.hidden_from_party,
          }
        : {}),
    });
  }

  function move(event, item) {
    event.preventDefault();
    const draft = drafts[item.id];
    mutate({
      operation: "moveItem",
      itemId: item.id,
      owner: dm
        ? draft.owner
        : item.owner === "party"
          ? characters[0]?.id
          : "party",
      quantity: draft.moveQuantity,
      reason: draft.moveReason,
    });
  }

  function remove(item) {
    if (window.confirm(`Delete ${item.name}? Its audit history is retained.`))
      mutate({ operation: "deleteItem", itemId: item.id });
  }

  onMount(() => {
    load();
    const timer = setInterval(() => {
      if (!busy) load();
    }, 4000);
    return () => clearInterval(timer);
  });
</script>

<section class="inventory" aria-label="Campaign inventory" aria-busy={busy}>
  {#if failure}<p role="alert">{failure}</p>{/if}
  {#if dm}
    <form
      class="give"
      onsubmit={(event) => {
        event.preventDefault();
        mutate({ operation: "giveItem", ...give });
      }}
    >
      <h2>Give item</h2>
      <label
        >Give to<select bind:value={give.owner} disabled={busy}>
          <option value="party">Party pool</option>
          {#each characters as pc (pc.id)}<option value={pc.id}
              >{pc.character_name}</option
            >{/each}
        </select></label
      >
      <label
        >Item name<input
          bind:value={give.name}
          maxlength="200"
          required
          disabled={busy}
        /></label
      >
      <label
        >Quantity<input
          type="number"
          bind:value={give.quantity}
          min="1"
          max="1000000"
          step="1"
          required
          disabled={busy}
        /></label
      >
      <label
        >Notes<textarea
          bind:value={give.notes}
          maxlength="20000"
          disabled={busy}></textarea></label
      >
      <label class="check"
        ><input
          type="checkbox"
          bind:checked={give.hidden_from_party}
          disabled={busy}
        />Hidden from party</label
      >
      <label
        >Reason (optional)<input
          bind:value={give.reason}
          maxlength="500"
          disabled={busy}
        /></label
      >
      {#if give.entity_id}<p>
          Linked knowledge <button
            type="button"
            class="chip"
            onclick={() => openKnowledge(give.entity_id)}
            >{give.name || "Open knowledge"}</button
          >
        </p>{/if}
      <button disabled={busy}>Give {give.name || "item"}</button>
    </form>
  {/if}
  {#if loading}<p role="status">Loading inventory…</p>{/if}
  {#each sections as section (section.owner)}
    {@const owned = items.filter((item) =>
      section.owner === "mine" ? item.is_mine : item.owner === section.owner,
    )}
    <section aria-label={section.name}>
      <h2>{section.name}</h2>
      {#if !loading && !owned.length}<p>No items here yet.</p>{/if}
      {#each owned as item (item.id)}
        {@const draft = drafts[item.id]}
        <article>
          <h3>{item.name} <span>× {item.quantity}</span></h3>
          {#if item.notes}<p class="notes">{item.notes}</p>{/if}
          {#if item.entity}<button
              type="button"
              class="chip"
              onclick={() => openKnowledge(item.entity.id)}
              aria-label={`Explore ${item.entity.name}`}
              >{item.entity.name}</button
            >{/if}
          {#if item.hidden_from_party}<p class="badge">
              Hidden from party
            </p>{/if}
          {#if dm || item.is_mine}
            <details>
              <summary
                >{dm ? "Edit" : "Adjust quantity for"} {item.name}</summary
              >
              <form onsubmit={(event) => update(event, item)}>
                {#if dm}
                  <label
                    >Name for {item.name}<input
                      bind:value={draft.name}
                      maxlength="200"
                      required
                      disabled={busy}
                    /></label
                  >
                  <label
                    >Notes for {item.name}<textarea
                      bind:value={draft.notes}
                      maxlength="20000"
                      disabled={busy}></textarea></label
                  >
                  <label class="check"
                    ><input
                      type="checkbox"
                      bind:checked={draft.hidden_from_party}
                      disabled={busy}
                    />Hide {item.name} from party</label
                  >
                {/if}
                <label
                  >Quantity for {item.name}<input
                    type="number"
                    bind:value={draft.quantity}
                    min="0"
                    max="1000000"
                    step="1"
                    required
                    disabled={busy}
                  /></label
                >
                <label
                  >Reason for {item.name} (optional)<input
                    bind:value={draft.reason}
                    maxlength="500"
                    disabled={busy}
                  /></label
                >
                <button disabled={busy}>Save {item.name}</button>
              </form>
            </details>
          {/if}
          {#if dm || (!item.hidden_from_party && characters[0]?.id && (item.is_mine || item.owner === "party"))}
            <form class="move" onsubmit={(event) => move(event, item)}>
              {#if dm}<label
                  >Move {item.name} to<select
                    bind:value={draft.owner}
                    disabled={busy}
                  >
                    <option value="party">Party pool</option>
                    {#each characters as pc (pc.id)}<option value={pc.id}
                        >{pc.character_name}</option
                      >{/each}
                  </select></label
                >{/if}
              <label
                >{item.owner === "party" && !dm ? "Take" : "Move"} quantity for {item.name}<input
                  type="number"
                  bind:value={draft.moveQuantity}
                  min="1"
                  max={item.quantity}
                  step="1"
                  required
                  disabled={busy || item.quantity === 0}
                /></label
              >
              <label
                >Move reason for {item.name} (optional)<input
                  bind:value={draft.moveReason}
                  maxlength="500"
                  disabled={busy}
                /></label
              >
              <button disabled={busy || item.quantity === 0}
                >{dm
                  ? `Move ${item.name}`
                  : item.owner === "party"
                    ? `Take ${item.name}`
                    : `Move ${item.name} to party pool`}</button
              >
            </form>
          {/if}
          {#if dm}<button
              type="button"
              disabled={busy}
              onclick={() => remove(item)}>Delete {item.name}</button
            >{/if}
          <details
            ontoggle={(event) => {
              historyOpen[item.id] = event.currentTarget.open;
              if (event.currentTarget.open) loadHistory(item.id);
            }}
          >
            <summary>History for {item.name}</summary>
            {#if !history[item.id]}<p role="status">
                Loading history…
              </p>{:else if !history[item.id].length}<p>
                No changes yet.
              </p>{:else}
              <ol>
                {#each history[item.id] as change (change.id)}
                  <li>
                    <strong>{change.action}</strong>: delta {change.delta},
                    quantity after {change.quantity_after}.
                    {#if change.changes?.owner}Owner: {change.changes.owner
                        .from || "none"} to {change.changes.owner.to}.{/if}
                    {#if change.reason}<p>{change.reason}</p>{/if}
                    <time datetime={change.created_at}
                      >{new Date(change.created_at).toLocaleString()}</time
                    >
                  </li>
                {/each}
              </ol>
            {/if}
          </details>
        </article>
      {/each}
    </section>
  {/each}
</section>

<style>
  .inventory {
    max-width: 760px;
    margin: 28px auto;
  }
  form,
  label {
    display: grid;
    gap: 8px;
  }
  form {
    margin: 16px 0;
  }
  article,
  .give {
    padding: 20px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    margin: 16px 0;
  }
  input,
  textarea,
  select,
  button {
    font: inherit;
    color: var(--grim-ink);
    background: var(--grim-surface);
    padding: 12px;
    border: 1px solid var(--grim-line);
    box-sizing: border-box;
  }
  input,
  textarea,
  select {
    width: 100%;
  }
  .check {
    display: flex;
    gap: 8px;
    align-items: center;
  }
  .check input {
    width: auto;
  }
  button,
  summary {
    cursor: pointer;
  }
  summary {
    padding: 12px 0;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .notes {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
  .badge,
  .chip,
  time {
    font-family: var(--font-mono);
    font-size: 0.875rem;
  }
  .badge {
    border: 1px solid var(--grim-line);
    padding: 8px;
    width: fit-content;
  }
  h3 {
    font-family: var(--grim-serif);
  }
  li {
    margin: 12px 0;
    overflow-wrap: anywhere;
  }
</style>
