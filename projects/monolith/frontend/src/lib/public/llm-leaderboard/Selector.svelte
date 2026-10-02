<script>
  // Which models the page compares. Presets cover the usual questions; the
  // drawer below them picks individual models, grouped by provider. The
  // drawer is a native <details>, closed by default, per the tier's rule for
  // long option lists.
  import {
    PRESETS,
    PROVIDERS,
    matchPreset,
    presetIds,
    provider,
    providerSlot,
    rank,
    shortName,
  } from "./model.js";

  let { models = [], selected, onchange } = $props();

  const active = $derived(matchPreset(models, selected));
  const groups = $derived.by(() => {
    const byProvider = new Map();
    for (const m of rank(models)) {
      const key = provider(m.id);
      if (!byProvider.has(key)) byProvider.set(key, []);
      byProvider.get(key).push(m);
    }
    const order = (p) => {
      const i = PROVIDERS.indexOf(p);
      return i === -1 ? PROVIDERS.length : i;
    };
    return [...byProvider.entries()].sort((a, b) => order(a[0]) - order(b[0]));
  });

  function toggle(id) {
    const next = new Set(selected);
    if (next.has(id)) {
      // Never empty the page: the last model stays.
      if (next.size === 1) return;
      next.delete(id);
    } else next.add(id);
    onchange(next);
  }
</script>

<section class="selector">
  <p class="sec-label">
    / Models
    <span class="aside num">{selected.size} of {models.length} shown</span>
  </p>
  <div class="row">
    <div class="seg" role="group" aria-label="Model presets">
      {#each PRESETS as preset}
        <button
          type="button"
          aria-pressed={active === preset.key}
          onclick={() => onchange(presetIds(models, preset.key))}
          >{preset.label}</button
        >
      {/each}
    </div>
    {#if !active}<span class="custom">custom selection</span>{/if}
  </div>
  <details>
    <summary>Choose models</summary>
    <div class="groups">
      {#each groups as [name, list]}
        <fieldset>
          <legend
            ><i class="sw" data-slot={providerSlot(list[0].id)}></i>{name}</legend
          >
          {#each list as m (m.id)}
            <label>
              <input
                type="checkbox"
                checked={selected.has(m.id)}
                onchange={() => toggle(m.id)}
              />
              <span>{shortName(m)}</span>
              {#if m.role === "anchor"}<em>ceiling</em>{:else if m.self_hosted}<em
                  >local</em
                >{/if}
            </label>
          {/each}
        </fieldset>
      {/each}
    </div>
  </details>
</section>

<style>
  .row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.6em 1em;
    align-items: center;
  }

  .custom {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  details {
    margin-top: 0.7em;
    border: 1px solid var(--stroke);
  }

  summary {
    display: flex;
    gap: 0.6em;
    align-items: center;
    padding: 0.4em 0.8em;
    background: var(--band);
    color: var(--ink);
    cursor: pointer;
    font-family: var(--font-code);
    font-size: 0.72rem;
    list-style: none;
  }

  summary::-webkit-details-marker {
    display: none;
  }

  summary::after {
    content: "+";
    margin-left: auto;
    color: var(--ink-2);
  }

  details[open] summary::after {
    content: "−";
  }

  details[open] summary {
    border-bottom: 1px solid var(--stroke);
  }

  .groups {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(13em, 1fr));
  }

  fieldset {
    min-width: 0;
    margin: 0;
    padding: 0.6em 0.8em 0.7em;
    border: 0;
    border-right: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
  }

  legend {
    display: flex;
    gap: 0.45em;
    align-items: center;
    float: left;
    width: 100%;
    margin-bottom: 0.35em;
    padding: 0;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  label {
    display: flex;
    clear: both;
    gap: 0.5em;
    align-items: baseline;
    padding: 0.12em 0;
    cursor: pointer;
    font-size: 0.82rem;
  }

  label:hover span {
    color: var(--accent-ink);
  }

  input {
    accent-color: var(--accent-ink);
    margin: 0;
  }

  em {
    margin-left: auto;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
    font-style: normal;
  }
</style>
