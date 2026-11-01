<script>
  import { presetSpeakers } from "./read-aloud.js";
  let { presets = [], endpoint, act, busy = false } = $props();
  // Colons never appear in speaker keys, so this cannot shadow a free label.
  const FIND = "npc:find";
  let selected = $state("narrator");
  let freeLabel = $state("");
  let lang = $state("");
  let names = $state("");
  let rate = $state(1);
  let pitch = $state(1);
  let query = $state("");
  let results = $state(null);
  let picked = $state(null);
  let searching = $state(false);
  let failure = $state("");
  let key = $derived(
    selected === "custom"
      ? freeLabel.trim()
      : selected === FIND
        ? ""
        : selected,
  );
  let speakers = $derived(presetSpeakers(presets));
  // Compared by value: the session poll hands over a new array every 2s, which
  // must not reload the form over unsaved edits. A save or delete changes the
  // stored preset, so this still reloads then.
  let saved = $derived(
    JSON.stringify(presets.find((row) => row.speaker_key === key) ?? null),
  );
  $effect(() => {
    const preset = JSON.parse(saved);
    lang = preset?.voice_hint?.lang || "";
    names = preset?.voice_hint?.names?.join(", ") || "";
    rate = preset?.rate ?? 1;
    pitch = preset?.pitch ?? 1;
  });
  // A name search over the DM's visible NPCs, so no poll carries the corpus.
  async function search(event) {
    event.preventDefault();
    searching = true;
    failure = "";
    try {
      const params = new URLSearchParams({ q: query.trim(), type: "npc" });
      const response = await fetch(`${endpoint}?${params}`);
      const result = await response.json();
      if (!response.ok) throw new Error(result.error);
      results = result.items;
    } catch (error) {
      failure = error.message || "Could not search NPCs.";
    } finally {
      searching = false;
    }
  }
  function pick(npc) {
    // Saved entity keys are canonical lowercase UUIDs.
    picked = { key: npc.id.toLowerCase(), name: npc.name };
    selected = picked.key;
    results = null;
    query = "";
  }
  function save(event) {
    event.preventDefault();
    act({
      operation: "saveVoice",
      speakerKey: key,
      voice_hint: {
        lang: lang.trim() || null,
        names: names
          .split(",")
          .map((name) => name.trim())
          .filter(Boolean),
      },
      rate: Number(rate),
      pitch: Number(pitch),
    });
  }
</script>

<details class="voice-presets">
  <summary>Voice presets</summary>
  <p>
    Voice names vary by device. Matching names take priority, then language,
    then the device default. Hints and free labels are visible to campaign
    members; use an NPC key for hidden identities.
  </p>
  {#if presets.length}<ul aria-label="Saved voice presets">
      {#each presets as preset}<li>
          <button
            type="button"
            class="secondary"
            onclick={() => {
              selected = preset.speaker_key;
            }}
            >{preset.speaker_key === "narrator"
              ? "Narrator"
              : preset.speaker_name || preset.speaker_key}</button
          >
        </li>{/each}
    </ul>{/if}
  <label
    >Preset speaker <select aria-label="Preset speaker" bind:value={selected}>
      <option value="narrator">Narrator</option>
      {#if picked && !speakers.some((row) => row.key === picked.key)}<option
          value={picked.key}>{picked.name}</option
        >{/if}
      {#each speakers as speaker}<option value={speaker.key}
          >{speaker.name}</option
        >{/each}
      <option value={FIND}>Find an NPC</option>
      <option value="custom">Free label</option>
    </select></label
  >
  {#if selected === FIND}
    <form onsubmit={search}>
      <label
        >NPC name <input
          aria-label="NPC name"
          type="search"
          bind:value={query}
          maxlength="200"
          required
        /></label
      >
      <button disabled={searching || !query.trim()}>Find NPC</button>
    </form>
    {#if results?.length}<ul aria-label="Matching NPCs">
        {#each results as npc}<li>
            <button type="button" class="secondary" onclick={() => pick(npc)}
              >{npc.name}</button
            >
          </li>{/each}
      </ul>{:else if results}<p>No NPC matches that name.</p>{/if}
    {#if failure}<p role="alert">{failure}</p>{/if}
  {:else}
    <form onsubmit={save}>
      {#if selected === "custom"}<label
          >Preset label <input
            aria-label="Preset label"
            bind:value={freeLabel}
            maxlength="64"
            pattern="(?!\.+$)[A-Za-z0-9 _'.\-]+"
            required
          /></label
        >{/if}
      <label
        >Language hint <input
          aria-label="Language hint"
          bind:value={lang}
          placeholder="en-GB"
          maxlength="35"
        /></label
      >
      <label
        >Preferred voice names <input
          aria-label="Preferred voice names"
          bind:value={names}
          placeholder="English, North"
        /></label
      >
      <p>Up to eight name substrings, separated by commas.</p>
      <div class="numbers">
        <label
          >Rate <input
            aria-label="Voice rate"
            type="number"
            min="0.5"
            max="2"
            step="0.1"
            bind:value={rate}
            required
          /></label
        >
        <label
          >Pitch <input
            aria-label="Voice pitch"
            type="number"
            min="0"
            max="2"
            step="0.1"
            bind:value={pitch}
            required
          /></label
        >
      </div>
      <div class="actions">
        <button disabled={busy || !key}>Save voice</button>
        <button
          type="button"
          class="secondary"
          disabled={busy || !presets.some((row) => row.speaker_key === key)}
          onclick={() => act({ operation: "deleteVoice", speakerKey: key })}
          >Delete voice</button
        >
      </div>
    </form>
  {/if}
</details>

<style>
  .voice-presets {
    margin-block: 20px;
  }
  summary {
    cursor: pointer;
  }
  p {
    color: var(--grim-text-dim);
    font-size: 0.85rem;
    margin-block: 12px;
    max-width: 70ch;
  }
  form {
    display: grid;
    gap: 12px;
  }
  label {
    display: grid;
    gap: 6px;
  }
  .voice-presets > label {
    margin-bottom: 12px;
  }
  .numbers,
  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }
  input,
  select {
    width: 100%;
  }
  input,
  select,
  button {
    font: inherit;
    color: var(--grim-ink);
    background: var(--grim-surface);
    padding: 12px;
    border: 1px solid var(--grim-line);
    box-sizing: border-box;
  }
  button {
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  ul {
    list-style: none;
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 0;
  }
</style>
