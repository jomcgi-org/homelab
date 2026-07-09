<script>
  // Public /app/firecracker: "boot once, restore forever", the
  // scroll-scrubbed explainer of the fc-invoke daemon. Zero /api fetches:
  // every number on the page comes from the baked static data/trace.js
  // module that FcScrollStory imports directly.
  import FcScrollStory from "$lib/public/fcstory/FcScrollStory.svelte";
</script>

<FcScrollStory />
