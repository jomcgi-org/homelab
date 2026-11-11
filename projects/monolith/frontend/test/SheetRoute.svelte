<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import PublicLayout from "../src/routes/public/+layout.svelte";
  import RootError from "../src/routes/+error.svelte";
  import Leaderboard from "../src/routes/public/app/llm-leaderboard/+page.svelte";
  import Artifact from "../src/routes/public/artifact/[id]/+page.svelte";

  // The real layout and error-node preload retain the same CSS as Kit.
  let { surface = "leaderboard", data = {} } = $props();
</script>

<RootLayout>
  {#if surface === "error"}
    <RootError />
  {:else}
    <PublicLayout {data}>
      {#if surface === "artifact"}<Artifact />
      {:else}<Leaderboard {data} />{/if}
    </PublicLayout>
  {/if}
</RootLayout>
