<script>
  import { page } from "$app/state";
  import { TechnicalDrawingChrome } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import Trail from "./Trail.svelte";

  let { children } = $props();

  // The trail is one box partitioned by rules, the same way a figure's
  // parts are: site, then the blog, then the post being read.
  const post = $derived(page.params.slug ? page.data.title : "");
</script>

<TechnicalDrawingChrome>
  {#snippet trail()}
    <Trail {post} />
  {/snippet}
</TechnicalDrawingChrome>

{@render children()}
