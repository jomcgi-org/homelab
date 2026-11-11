<script>
  // Public /ember/firecracker: "boot once, restore forever", the
  // sheet explainer of the fc-invoke daemon. Zero /api fetches:
  // every number on the page comes from the baked static data/trace.js
  // module that FcScrollStory imports directly.
  import FcScrollStory from "$lib/public/fcstory/FcScrollStory.svelte";
</script>

<svelte:head>
  <title>Boot once, restore forever · Firecracker microVMs · jomcgi.dev</title>
  <meta
    name="description"
    content="How this site runs untrusted code: a Firecracker microVM is booted and frozen once, then every request restores it from disk in about 22 milliseconds. Every number is a real measurement, baked in at build time."
  />
  <!-- The complete story is server-rendered in normal flow for every reader.
       Only the replay controls require JavaScript. -->
  <noscript>
    <style>
      .fcstory .replay-controls {
        display: none !important;
      }
    </style>
  </noscript>
</svelte:head>

<FcScrollStory />
