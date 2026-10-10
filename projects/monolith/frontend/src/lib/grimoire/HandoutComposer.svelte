<script>
  import HandoutText from "./HandoutText.svelte";
  import {
    HANDOUT_IMAGE_MAX_BYTES,
    HANDOUT_MARKDOWN_MAX,
    HANDOUT_TITLE_MAX,
    composeHandout,
  } from "./handout.js";

  // `send(message)` posts the composed handout and resolves truthy once the
  // backend accepted it, so a failed send keeps the draft.
  let { campaignId, characters = [], busy = false, send } = $props();
  const uid = $props.id();
  let title = $state("");
  let markdown = $state("");
  let toTable = $state(true);
  let picked = $state([]);
  let image = $state(null);
  let uploading = $state(false);
  let problem = $state("");
  let pending = null;

  const audience = $derived(toTable ? "table" : picked.map((id) => `pc:${id}`));
  const ready = $derived(
    title.trim().length > 0 && (toTable || picked.length > 0),
  );

  async function chooseImage(event) {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = "";
    if (!file) return;
    problem = "";
    if (file.size > HANDOUT_IMAGE_MAX_BYTES) {
      problem = "Handout images can be at most 5 MiB.";
      return;
    }
    uploading = true;
    try {
      const form = new FormData();
      form.set("file", file);
      const response = await fetch(
        `/grimoire/campaigns/${campaignId}/handouts/uploads`,
        {
          method: "POST",
          body: form,
        },
      );
      const result = await response.json().catch(() => ({}));
      if (!response.ok)
        throw new Error(result.error || "Could not upload that image.");
      image = { ...result, name: file.name };
    } catch (error) {
      problem = error.message || "Could not upload that image.";
    } finally {
      uploading = false;
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (!ready || busy || uploading) return;
    const message = composeHandout({
      title,
      markdown,
      audience,
      image: image ? { source: "upload", key: image.key } : null,
    });
    // A retry of the same content reuses its request id so it cannot post twice.
    const signature = JSON.stringify(message);
    if (pending?.signature !== signature)
      pending = { signature, id: crypto.randomUUID() };
    problem = "";
    if (await send({ ...message, requestId: pending.id })) {
      title = "";
      markdown = "";
      toTable = true;
      picked = [];
      image = null;
      pending = null;
    }
  }
</script>

<form class="handout-composer" onsubmit={submit} aria-label="Send a handout">
  <label for={`${uid}-title`}>Handout title</label>
  <input
    id={`${uid}-title`}
    bind:value={title}
    maxlength={HANDOUT_TITLE_MAX}
    placeholder="A map of the pass"
    required
  />
  <label for={`${uid}-markdown`}>Handout text</label>
  <textarea
    id={`${uid}-markdown`}
    bind:value={markdown}
    maxlength={HANDOUT_MARKDOWN_MAX}
    rows="5"
    placeholder="Markdown: **bold**, lists, quotes"></textarea>

  <fieldset>
    <legend>Send to</legend>
    <label
      ><input type="checkbox" bind:checked={toTable} />Everyone at the table</label
    >
    {#if !toTable}
      {#each characters as character (character.id)}
        <label
          ><input
            type="checkbox"
            value={character.id}
            bind:group={picked}
          />{character.character_name}</label
        >
      {/each}
    {/if}
  </fieldset>

  <div class="image">
    <label for={`${uid}-image`}>Image (optional, up to 5 MiB)</label>
    <input
      id={`${uid}-image`}
      type="file"
      accept="image/png,image/jpeg,image/gif,image/webp"
      disabled={uploading}
      onchange={chooseImage}
    />
    {#if uploading}<p role="status">Uploading…</p>{/if}
    {#if image}<p role="status">
        Uploaded {image.name} ({image.content_type}, {Math.ceil(
          image.size / 1024,
        )} KB).
        <button type="button" class="secondary" onclick={() => (image = null)}
          >Remove image</button
        >
      </p>{/if}
  </div>

  {#if problem}<p role="alert" class="problem">{problem}</p>{/if}

  <section class="preview" aria-label="Handout preview">
    <h3>{title.trim() || "Untitled handout"}</h3>
    <HandoutText {markdown} />
  </section>

  <button disabled={busy || uploading || !ready}>Send handout</button>
</form>

<style>
  .handout-composer {
    display: grid;
    gap: 0.5rem;
    margin-top: 0.75rem;
  }
  fieldset {
    border: 1px solid var(--grim-line);
    display: grid;
    gap: 0.25rem;
  }
  .preview {
    border: 1px dashed var(--grim-line);
    padding: 0.75rem;
  }
  .preview h3 {
    font-family: var(--grim-serif, Georgia, serif);
    margin: 0 0 0.5rem;
  }
  .problem {
    font-weight: 700;
  }
</style>
