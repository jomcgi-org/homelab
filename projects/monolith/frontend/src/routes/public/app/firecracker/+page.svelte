<script>
  // Public /app/firecracker: "boot once, restore forever", the
  // scroll-scrubbed explainer of the fc-invoke daemon. Zero /api fetches:
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
  <!-- No-JS: the scroll-scrubbed stage cannot animate, so reveal the static
       stacked story instead. JS visitors never hit this, so there is no
       display swap on hydration (that swap was the old layout-shift source). -->
  <noscript>
    <style>
      .scroller {
        display: none !important;
      }
      .static-story {
        display: block !important;
      }
    </style>
  </noscript>
</svelte:head>

<FcScrollStory />
