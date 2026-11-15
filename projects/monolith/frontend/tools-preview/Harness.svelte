<script>
  // Same root/private layouts and preloaded root-error CSS as production.
  // No server loaders, credentials, or private records enter the harness.
  import "../src/lib/public/styles/design-system.css";
  import "../preview/fonts.css";
  import Root from "../src/routes/+layout.svelte";
  import Private from "../src/routes/private/+layout.svelte";
  import Review from "../src/routes/private/review/+page.svelte";
  import Chat from "../src/routes/private/chat/+page.svelte";
  import Notes from "../src/routes/private/notes/+page.svelte";
  import Qwen from "../src/routes/private/qwen/+page.svelte";
  import Grimoire from "../src/routes/private/grimoire/+page.svelte";
  import { reviewData, notesData, grimoireData } from "./fixtures.js";
  import { page } from "./navigation.js";
  let { view = "review", data, form } = $props();
  $effect(() => {
    page.set({
      url: new URL(`https://private.fixture.invalid/${view}`),
      data: {},
      status: 200,
      error: null,
    });
  });
</script>

<Root
  ><Private>
    {#if view === "review"}<Review data={data ?? reviewData} />
    {:else if view === "chat"}<Chat />
    {:else if view === "notes"}<Notes data={data ?? notesData} />
    {:else if view === "qwen"}<Qwen />
    {:else if view === "grimoire"}<Grimoire
        data={data ?? grimoireData()}
        {form}
      />{/if}
  </Private></Root
>
