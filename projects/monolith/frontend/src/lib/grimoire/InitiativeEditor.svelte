<script>
  import { untrack } from "svelte";

  // `initiative` is the DM view from the server. The rows below are a local
  // draft: a poll never overwrites them while the DM has unsaved edits.
  let {
    initiative = null,
    characters = [],
    events = [],
    members = [],
    busy = false,
    act,
  } = $props();

  let nextKey = 0;
  let rows = $state([]);
  let activeKey = $state(null);
  let hiddenDisplay = $state("mask");
  let dirty = $state(false);
  let note = $state("");

  const toRows = (entries) =>
    (entries || []).map((entry) => ({
      key: (nextKey += 1),
      label: entry.label,
      player_character_id: entry.player_character_id || "",
      initiative: entry.initiative,
      hidden: entry.hidden === true,
    }));

  $effect(() => {
    const view = initiative;
    if (dirty) return;
    untrack(() => {
      rows = toRows(view?.entries);
      activeKey = rows[view?.active_index ?? -1]?.key ?? null;
      hiddenDisplay = view?.hidden_display || "mask";
    });
  });

  let saved = $derived(initiative?.entries?.length > 0);
  let invalid = $derived(
    rows.some(
      (row) =>
        !row.label.trim() ||
        row.initiative === "" ||
        row.initiative === null ||
        !Number.isInteger(Number(row.initiative)),
    ),
  );

  function edit(change) {
    change();
    dirty = true;
    note = "";
  }

  const addRow = () =>
    edit(() => {
      rows.push({
        key: (nextKey += 1),
        label: "",
        player_character_id: "",
        initiative: 10,
        hidden: false,
      });
    });

  const removeRow = (index) =>
    edit(() => {
      rows.splice(index, 1);
    });

  const move = (index, step) =>
    edit(() => {
      const target = index + step;
      if (target < 0 || target >= rows.length) return;
      [rows[index], rows[target]] = [rows[target], rows[index]];
    });

  function pickCharacter(row) {
    edit(() => {
      const character = characters.find(
        (item) => item.id === row.player_character_id,
      );
      if (!character) return;
      row.hidden = false;
      if (!row.label.trim()) row.label = character.character_name;
    });
  }

  // Highest first. Array sort is stable, so ties keep their current order.
  const sortByInitiative = () =>
    edit(() => {
      rows.sort((a, b) => Number(b.initiative) - Number(a.initiative));
    });

  // The latest unretracted initiative roll authored for each character.
  function useRolls() {
    const found = new Map();
    for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
      if (event.kind !== "roll" || event.retracted_at) continue;
      if (!/initiative/i.test(event.body?.label || "")) continue;
      const pc = members.find(
        (member) => member.id === event.author_member_id,
      )?.player_character_id;
      if (pc && Number.isInteger(event.body.total))
        found.set(pc, event.body.total);
    }
    let filled = 0;
    edit(() => {
      for (const row of rows)
        if (found.has(row.player_character_id)) {
          row.initiative = found.get(row.player_character_id);
          filled += 1;
        }
    });
    note = filled
      ? `Filled ${filled} initiative ${filled === 1 ? "value" : "values"} from rolls.`
      : "No initiative rolls found for these characters.";
  }

  async function save() {
    const index = rows.findIndex((row) => row.key === activeKey);
    const result = await act({
      operation: "initiativeSet",
      entries: rows.map((row) => ({
        label: row.label.trim(),
        player_character_id: row.player_character_id || null,
        initiative: Number(row.initiative),
        hidden: row.player_character_id ? false : row.hidden,
      })),
      hiddenDisplay,
      activeIndex: Math.max(index, 0),
      round: initiative?.round || 1,
    });
    if (result) {
      dirty = false;
      note = "Initiative order saved.";
    }
  }

  const advance = (direction) =>
    act({ operation: "initiativeAdvance", direction });

  async function end() {
    if (!confirm("End this encounter? The turn order will be cleared.")) return;
    if (await act({ operation: "initiativeEnd" })) {
      dirty = false;
      note = "";
    }
  }
</script>

<section class="initiative-editor" aria-label="Initiative order">
  <ol class="rows">
    {#each rows as row, index (row.key)}
      <li class="row" class:active={row.key === activeKey}>
        <label
          >Name<input
            bind:value={row.label}
            maxlength="80"
            oninput={() => (dirty = true)}
          /></label
        >
        <label
          >Character<select
            bind:value={row.player_character_id}
            onchange={() => pickCharacter(row)}
          >
            <option value="">NPC</option>
            {#each characters as character}<option value={character.id}
                >{character.character_name}</option
              >{/each}
          </select></label
        >
        <label
          >Initiative<input
            type="number"
            step="1"
            bind:value={row.initiative}
            oninput={() => (dirty = true)}
          /></label
        >
        <label class="check"
          ><input
            type="checkbox"
            bind:checked={row.hidden}
            disabled={Boolean(row.player_character_id)}
            onchange={() => (dirty = true)}
          />Hidden</label
        >
        <div class="row-actions">
          <button
            type="button"
            class="secondary"
            disabled={index === 0}
            aria-label={`Move ${row.label || "entry"} up`}
            onclick={() => move(index, -1)}>Up</button
          >
          <button
            type="button"
            class="secondary"
            disabled={index === rows.length - 1}
            aria-label={`Move ${row.label || "entry"} down`}
            onclick={() => move(index, 1)}>Down</button
          >
          <button
            type="button"
            class="secondary"
            aria-label={`Remove ${row.label || "entry"}`}
            onclick={() => removeRow(index)}>Remove</button
          >
        </div>
      </li>
    {/each}
  </ol>
  {#if !rows.length}<p class="hint">No one is in the order yet.</p>{/if}

  <fieldset>
    <legend>Hidden entries for players</legend>
    <label
      ><input
        type="radio"
        name="hidden-display"
        value="mask"
        bind:group={hiddenDisplay}
        onchange={() => (dirty = true)}
      />Mask as ???</label
    >
    <label
      ><input
        type="radio"
        name="hidden-display"
        value="omit"
        bind:group={hiddenDisplay}
        onchange={() => (dirty = true)}
      />Omit entirely</label
    >
  </fieldset>

  <div class="actions">
    <button type="button" class="secondary" onclick={addRow}>Add row</button>
    <button
      type="button"
      class="secondary"
      disabled={rows.length < 2}
      onclick={sortByInitiative}>Sort by initiative</button
    >
    <button
      type="button"
      class="secondary"
      disabled={!rows.length}
      onclick={useRolls}>Use rolls</button
    >
    <button type="button" disabled={busy || invalid} onclick={save}
      >Save order</button
    >
  </div>
  <div class="actions" aria-label="Turn controls">
    <button
      type="button"
      class="secondary"
      disabled={busy || dirty || !saved}
      onclick={() => advance("previous")}>Previous turn</button
    >
    <button
      type="button"
      disabled={busy || dirty || !saved}
      onclick={() => advance("next")}>Next turn</button
    >
    <button
      type="button"
      class="secondary"
      disabled={busy || dirty || !saved}
      onclick={end}>End encounter</button
    >
  </div>
  {#if dirty && saved}<p class="hint">
      Save your changes to use the turn controls.
    </p>{/if}
  <p class="hint" role="status">{note}</p>
</section>

<style>
  .rows {
    display: grid;
    gap: 10px;
    margin: 0 0 12px;
    padding: 0;
    list-style: none;
  }
  .row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
    gap: 8px;
    align-items: end;
    padding: 10px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
  }
  .row.active {
    border-left: 4px solid var(--grim-accent);
  }
  .row label {
    display: grid;
    gap: 4px;
    font-size: 13px;
  }
  .row .check {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .row .check input {
    width: auto;
  }
  .row-actions,
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin: 8px 0;
  }
  fieldset {
    border: 1px solid var(--grim-line);
    margin: 8px 0;
  }
  fieldset label {
    display: inline-flex;
    gap: 6px;
    align-items: center;
    margin-right: 16px;
  }
  fieldset input {
    width: auto;
  }
  .hint {
    margin: 4px 0;
    font-size: 13px;
    color: var(--grim-ink-soft);
  }
  button {
    min-height: 44px;
    padding: 8px 16px;
    font: inherit;
    border: 1px solid var(--grim-accent);
    background: var(--grim-accent);
    color: var(--grim-on-accent);
    cursor: pointer;
  }
  button.secondary {
    background: transparent;
    color: var(--grim-accent);
  }
  button:disabled {
    opacity: 0.55;
    cursor: default;
  }
  input,
  select {
    font: inherit;
    color: inherit;
    padding: 10px;
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    width: 100%;
    box-sizing: border-box;
  }
  button:focus-visible,
  input:focus-visible,
  select:focus-visible {
    outline: 3px solid var(--grim-accent);
    outline-offset: 2px;
  }
</style>
