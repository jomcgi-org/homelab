<script>
  import RootLayout from "../src/routes/+layout.svelte";
  import FriendsLayout from "../src/routes/friends/+layout.svelte";
  import Moving from "../src/routes/friends/moving/+page.svelte";
  // Kit preloads this node, including its legacy stylesheet, on friends too.
  import RootError from "../src/routes/+error.svelte";
  let { data, rootError = false } = $props();
</script>

<RootLayout>
  {#if rootError}<RootError />{:else}<FriendsLayout
      ><Moving {data} /></FriendsLayout
    >{/if}
</RootLayout>
