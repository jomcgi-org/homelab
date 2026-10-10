<script>
  import { onMount, untrack } from "svelte";
  import {
    createEventReader,
    createSpeaker,
    loadReadAloudSettings,
    readAloudDefaults,
    saveReadAloudSettings,
  } from "./read-aloud.js";
  let { campaignId, role, pcId, events, voices = [], micPauser } = $props();
  let settings = $state(untrack(() => readAloudDefaults(role)));
  let speaker = $state.raw(null);
  let speaking = $state(false);
  const reader = createEventReader();
  let storage;

  onMount(() => {
    try {
      storage = globalThis.localStorage;
    } catch {
      /* Storage may be disabled. */
    }
    settings = loadReadAloudSettings(storage, campaignId, role);
    reader.select(events, { role, pcId, ...settings });
    speaker = createSpeaker({
      micPauser,
      onSpeakingChange: (value) => {
        speaking = value;
      },
    });
    return () => speaker.dispose();
  });
  $effect(() => {
    if (speaker)
      speaker.deliver(
        reader.select(events, { role, pcId, ...settings }),
        voices,
      );
  });
  function toggle(key, value) {
    settings[key] = value;
    speaker.stop();
    saveReadAloudSettings(storage, campaignId, settings);
  }
</script>

{#if speaker?.available}
  <section aria-label="Read aloud" class="read-aloud">
    <div class="choices">
      <label
        ><input
          type="checkbox"
          checked={settings.narration}
          onchange={(event) => toggle("narration", event.currentTarget.checked)}
        />Read DM narration</label
      >
      {#if role !== "dm"}<label
          ><input
            type="checkbox"
            checked={settings.reveals}
            onchange={(event) => toggle("reveals", event.currentTarget.checked)}
          />Read my reveals</label
        >{/if}
      <button
        type="button"
        class="secondary"
        disabled={!speaking}
        onclick={() => speaker.stop()}>Stop read-aloud</button
      >
    </div>
    <p>
      Saved on this device for this campaign. For a table speaker, the DM turns
      narration off and one table member turns it on. Private messages and
      reveals never play on the DM room speaker.
    </p>
  </section>
{/if}

<style>
  .read-aloud {
    margin-block: 16px;
  }
  .choices {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 16px;
  }
  label {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  button {
    font: inherit;
    color: var(--grim-ink);
    background: var(--grim-surface);
    padding: 12px;
    border: 1px solid var(--grim-line);
    cursor: pointer;
  }
  button:disabled {
    opacity: 0.5;
    cursor: default;
  }
  p {
    margin-block: 8px;
    font-size: 0.85rem;
    color: var(--grim-text-dim);
    max-width: 70ch;
  }
</style>
