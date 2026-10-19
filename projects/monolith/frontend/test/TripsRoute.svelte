<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import PublicLayout from "../src/routes/public/+layout.svelte";
  import Index from "../src/routes/public/app/trips/+page.svelte";
  import Trip from "../src/routes/public/app/trips/[slug]/+page.svelte";
  import Day from "../src/routes/public/app/trips/[slug]/day/[day]/+page.svelte";
  import Timeline from "../src/routes/public/app/trips/[slug]/timeline/+page.svelte";
  // Kit preloads the root error stylesheet on these routes too.
  import "../src/lib/public/styles/design-system.css";
  let { surface = "index", data } = $props();
</script>

<RootLayout>
  <PublicLayout {data}>
    {#if surface === "index"}<Index {data} />
    {:else if surface === "trip"}<Trip {data} />
    {:else if surface === "day"}<Day {data} />
    {:else}<Timeline {data} />{/if}
  </PublicLayout>
</RootLayout>
