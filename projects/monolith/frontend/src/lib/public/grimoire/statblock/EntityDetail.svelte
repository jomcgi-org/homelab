<script>
  // Dispatcher: pick a typed renderer by entity_type, falling back to Generic
  // for anything without a bespoke layout (location/npc/faction/deity/item).
  // Unlike the private EntityDetail, the public /entities/{id} payload is
  // always the full flattened spine (no grants, no partial/revealed_details
  // path exists in the no-auth public tier).
  import Creature from "./Creature.svelte";
  import Spell from "./Spell.svelte";
  import Generic from "./Generic.svelte";

  let { entity } = $props();

  const RENDERERS = { creature: Creature, spell: Spell };
  const Renderer = $derived(RENDERERS[entity?.entity_type] ?? Generic);
</script>

<Renderer data={entity} />
