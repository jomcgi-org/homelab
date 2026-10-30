<script>
  import RootLayout from "../../src/routes/+layout.svelte";
  import PublicLayout from "../../src/routes/public/+layout.svelte";
  import Page from "../../src/routes/public/app/hikes/+page.svelte";
  // Kit preloads this node and its legacy CSS; keep the same cascade in tests.
  import "../../src/routes/+error.svelte";
  let { data } = $props();
</script>

<RootLayout><PublicLayout {data}><Page {data} /></PublicLayout></RootLayout>
