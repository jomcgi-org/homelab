<script>
  let { presets = [], npcs = [], act, busy = false } = $props();
  let selected = $state("narrator");
  let freeLabel = $state("");
  let lang = $state("");
  let names = $state("");
  let rate = $state(1);
  let pitch = $state(1);
  let key = $derived(selected === "custom" ? freeLabel.trim() : selected);
  let labels = $derived(
    presets.filter(
      (row) =>
        row.speaker_key !== "narrator" &&
        !npcs.some((npc) => npc.id === row.speaker_key),
    ),
  );
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
              : npcs.find((npc) => npc.id === preset.speaker_key)?.name ||
                preset.speaker_key}</button
          >
        </li>{/each}
    </ul>{/if}
  <form onsubmit={save}>
    <label
      >Preset speaker <select aria-label="Preset speaker" bind:value={selected}>
        <option value="narrator">Narrator</option>
        {#each npcs as npc}<option value={npc.id}>{npc.name}</option>{/each}
        {#each labels as preset}<option value={preset.speaker_key}
            >{preset.speaker_key}</option
          >{/each}
        <option value="custom">Free label</option>
      </select></label
    >
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
