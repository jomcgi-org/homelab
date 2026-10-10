<script>
  import HandoutText from "./HandoutText.svelte";
  import { handoutImageUrl } from "./handout.js";

  let {
    handout,
    campaignId,
    sessionId,
    dm = false,
    busy = false,
    pin,
    openKnowledge,
  } = $props();
  const retracted = $derived(Boolean(handout.retracted_at));
  const title = $derived(handout.body?.title || "Handout");
  // The image is only ever the members-only proxy for this event; the stored
  // object key never reaches the page.
  const imageUrl = $derived(
    handout.body?.image
      ? handoutImageUrl(campaignId, sessionId, handout.id)
      : null,
  );
</script>

<div class="handout-card">
  {#if retracted}
    <p class="retracted">This handout was retracted.</p>
  {:else}
    <h3>{title}</h3>
    {#if imageUrl}<img
        src={imageUrl}
        alt={`Handout image: ${title}`}
        loading="lazy"
      />{/if}
    <HandoutText markdown={handout.body?.markdown} />
    {#if handout.body?.entity_id && openKnowledge}<button
        type="button"
        class="secondary"
        onclick={() => openKnowledge(handout.body.entity_id)}
        >Explore linked knowledge</button
      >{/if}
    {#if !dm && pin}<button
        type="button"
        class="secondary"
        disabled={busy}
        aria-label={`Pin ${title} to my notes`}
        onclick={() => pin(handout)}>Pin to my notes</button
      >{/if}
  {/if}
</div>

<style>
  .handout-card {
    border: 1px solid var(--grim-line);
    background: var(--grim-surface);
    padding: 1rem;
  }
  h3 {
    font-family: var(--grim-serif, Georgia, serif);
    margin: 0 0 0.5rem;
  }
  img {
    display: block;
    max-width: 100%;
    height: auto;
    margin: 0.5rem 0;
  }
  .retracted {
    font-weight: 700;
    margin: 0;
  }
  button {
    margin-top: 0.5rem;
    margin-right: 0.5rem;
  }
</style>
