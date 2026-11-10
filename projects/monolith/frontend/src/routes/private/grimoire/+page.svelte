<script>
  import CharacterSheets from "$lib/grimoire/CharacterSheets.svelte";
  let { data, form } = $props();
</script>

<CharacterSheets {data} {form} />
