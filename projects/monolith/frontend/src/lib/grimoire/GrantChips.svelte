<script>
  import { apiFetch } from "$lib/grimoire/api.js";
  import {
    META_FIELDS,
    formatFieldName,
    scalarToText,
  } from "$lib/grimoire/format.js";

  // DM-only contextual grant editor. Per-character scope chips write straight
  // to the grant endpoints; partial opens a field picker over the entity's
  // populated fields and builds revealed_details from the real values.
  let { campaignId, entityId, entity, characters } = $props();

  const SCOPES = [
    { key: "none", label: "none" },
    { key: "name_only", label: "name only" },
    { key: "partial", label: "partial" },
    { key: "full", label: "full" },
  ];

  let grants = $state([]); // full grant rows for THIS entity
  let error = $state("");
  let pickerFor = $state(null); // character id while choosing partial fields
  let picked = $state(new Set());

  // Fields eligible for a partial reveal: the entity's populated, non-meta
  // scalar-or-structured fields (the DM sees the full entity here).
  const revealableFields = $derived(
    Object.keys(entity ?? {}).filter(
      (k) => !META_FIELDS.has(k) && entity[k] != null && entity[k] !== "",
    ),
  );

  async function load() {
    try {
      const all = await apiFetch(`/campaigns/${campaignId}/grants`);
      grants = all.filter((g) => g.entity_id === entityId);
    } catch (e) {
      error = e.message;
    }
  }

  $effect(() => {
    // Reload whenever the entity changes.
    entityId;
    load();
  });

  const grantFor = (charId) => grants.find((g) => g.player_character_id === charId);
  const scopeOf = (charId) => grantFor(charId)?.grant_scope ?? "none";

  async function setScope(charId, scope) {
    error = "";
    const existing = grantFor(charId);
    try {
      if (scope === "none") {
        if (existing) {
          await apiFetch(`/campaigns/${campaignId}/grants/${existing.id}`, {
            method: "DELETE",
          });
        }
      } else if (scope === "partial") {
        // Defer the write until the field picker confirms.
        picked = new Set(Object.keys(existing?.revealed_details ?? {}));
        pickerFor = charId;
        return;
      } else if (existing) {
        await apiFetch(`/campaigns/${campaignId}/grants/${existing.id}`, {
          method: "PATCH",
          body: JSON.stringify({ grant_scope: scope }),
        });
      } else {
        await apiFetch(`/campaigns/${campaignId}/grants`, {
          method: "POST",
          body: JSON.stringify({
            entity_id: entityId,
            player_character_id: charId,
            grant_scope: scope,
          }),
        });
      }
      await load();
    } catch (e) {
      error = e.message;
    }
  }

  function toggleField(key) {
    const next = new Set(picked);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    picked = next;
  }

  async function confirmPartial() {
    const charId = pickerFor;
    error = "";
    const revealed = {};
    for (const key of picked) revealed[key] = entity[key];
    const existing = grantFor(charId);
    try {
      if (existing) {
        await apiFetch(`/campaigns/${campaignId}/grants/${existing.id}`, {
          method: "PATCH",
          body: JSON.stringify({
            grant_scope: "partial",
            revealed_details: revealed,
          }),
        });
      } else {
        await apiFetch(`/campaigns/${campaignId}/grants`, {
          method: "POST",
          body: JSON.stringify({
            entity_id: entityId,
            player_character_id: charId,
            grant_scope: "partial",
            revealed_details: revealed,
          }),
        });
      }
      pickerFor = null;
      await load();
    } catch (e) {
      error = e.message;
    }
  }
</script>

<section class="grants">
  <h3 class="grim-smallcaps head">Reveals</h3>

  {#if characters.length === 0}
    <p class="hint">No characters in this campaign yet.</p>
  {:else}
    <ul class="rows">
      {#each characters as char (char.id)}
        <li class="row">
          <span class="char">{char.character_name}</span>
          <div class="chips">
            {#each SCOPES as scope (scope.key)}
              <button
                class="chip"
                class:chip--active={scopeOf(char.id) === scope.key}
                onclick={() => setScope(char.id, scope.key)}
              >
                {scope.label}
              </button>
            {/each}
          </div>

          {#if pickerFor === char.id}
            <div class="picker">
              <p class="picker-head">Reveal which fields to {char.character_name}?</p>
              <div class="picker-fields">
                {#each revealableFields as key (key)}
                  <label class="field">
                    <input
                      type="checkbox"
                      checked={picked.has(key)}
                      onchange={() => toggleField(key)}
                    />
                    <span class="field-name">{formatFieldName(key)}</span>
                    <span class="field-val">{scalarToText(entity[key]).slice(0, 40)}</span>
                  </label>
                {/each}
              </div>
              <div class="picker-actions">
                <button class="btn" onclick={confirmPartial}>Save reveal</button>
                <button class="btn btn--ghost" onclick={() => (pickerFor = null)}>
                  Cancel
                </button>
              </div>
            </div>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}

  {#if error}<p class="error">{error}</p>{/if}
</section>

<style>
  .grants {
    border: 1px solid var(--grim-paper-line);
    padding: 1rem;
  }

  .head {
    font-size: 0.9rem;
    color: var(--grim-accent);
    margin-bottom: 0.75rem;
  }

  .rows {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem 0.75rem;
  }

  .char {
    font-family: var(--grim-serif);
    font-weight: 700;
    min-width: 8rem;
  }

  .chips {
    display: flex;
    gap: 0.3rem;
    flex-wrap: wrap;
  }

  .chip {
    font-family: var(--font-mono);
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    min-height: 2.25rem;
    padding: 0.3rem 0.55rem;
    background: var(--bg);
    color: var(--fg-secondary);
    border: var(--border-thin);
    cursor: pointer;
  }

  .chip--active {
    background: var(--grim-accent);
    border-color: var(--grim-accent);
    color: #fff;
  }

  .picker {
    flex-basis: 100%;
    border: 1px dashed var(--grim-paper-line);
    padding: 0.75rem;
    margin-top: 0.25rem;
  }

  .picker-head {
    font-size: 0.72rem;
    color: var(--fg-secondary);
    margin-bottom: 0.5rem;
  }

  .picker-fields {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
    margin-bottom: 0.6rem;
  }

  .field {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    font-size: 0.8rem;
  }

  .field-name {
    font-family: var(--font-mono);
    font-size: 0.66rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--fg-tertiary);
    min-width: 6rem;
  }

  .field-val {
    font-family: var(--grim-serif);
    color: var(--fg-secondary);
  }

  .picker-actions {
    display: flex;
    gap: 0.5rem;
  }

  .btn {
    font-family: var(--font-mono);
    font-size: 0.72rem;
    min-height: 2.5rem;
    padding: 0.4rem 0.9rem;
    background: var(--grim-accent);
    color: #fff;
    border: none;
    cursor: pointer;
  }

  .btn--ghost {
    background: var(--bg);
    color: var(--fg-secondary);
    border: var(--border-thin);
  }

  .hint {
    font-size: 0.78rem;
    color: var(--fg-tertiary);
  }

  .error {
    color: var(--danger);
    font-size: 0.78rem;
    margin-top: 0.5rem;
  }
</style>
