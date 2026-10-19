<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import PublicLayout from "../src/routes/public/+layout.svelte";
  import Page from "../src/routes/public/app/dr-jobs/+page.svelte";
  let { data } = $props();
</script>

<RootLayout><PublicLayout {data}><Page {data} /></PublicLayout></RootLayout>
