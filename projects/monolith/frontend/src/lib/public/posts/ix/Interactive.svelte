<script>
  import { onMount } from "svelte";
  import { figures } from "./index.js";

  // Server render and no-JS: the post's own blocks. Hydrated: the figure.
  let { name, fallback } = $props();
  let Figure = $state(null);
  onMount(async () => {
    const load = figures[name];
    if (load) Figure = (await load()).default;
  });
</script>

{#if Figure}<Figure />{:else}{@html fallback}{/if}
