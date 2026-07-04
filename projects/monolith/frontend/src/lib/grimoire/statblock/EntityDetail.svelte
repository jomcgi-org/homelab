<script>
  import Creature from "./Creature.svelte";
  import Spell from "./Spell.svelte";
  import Generic from "./Generic.svelte";

  let { entity } = $props();

  // A partial grant to a player is {id, entity_type, name, revealed_details}
  // with no full spine. Render the revealed subset through the same typed
  // renderer so partial knowledge looks like a redacted stat block, never JSON.
  const partial = $derived(
    !!(entity?.revealed_details && !("source_type" in entity)),
  );

  const data = $derived(
    partial
      ? {
          name: entity.name,
          entity_type: entity.entity_type,
          ...(entity.revealed_details ?? {}),
        }
      : entity,
  );

  const RENDERERS = { creature: Creature, spell: Spell };
  const Renderer = $derived(RENDERERS[data?.entity_type] ?? Generic);
</script>

{#if partial}
  <p class="partial-note">
    Partial knowledge — only what the DM has revealed is shown.
  </p>
{/if}

<Renderer {data} />

<style>
  .partial-note {
    font-family: var(--font-mono);
    font-size: 0.68rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: var(--grim-accent);
    margin-bottom: 0.5rem;
  }
</style>
