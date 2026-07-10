<script>
  // Wraps the routed page content and plays a short slide+fade whenever the
  // top-level section changes (library / world / chat / a book reader
  // segment). Keyed on that section string, not the full pathname, so moving
  // between pages inside the same section (e.g. one book chunk to the next)
  // does not retrigger the transition.
  //
  // transform/opacity only, per the grimoire motion rule (see theme.css
  // .grim-stagger): never animate layout properties. Under
  // prefers-reduced-motion the swap is an instant cut, matching the pattern
  // used by MiniConstellation.svelte. Svelte transitions run off the main
  // thread's layout pass and never block input: navigation itself is
  // unaffected regardless of how long the CSS transition takes.
  import { fly } from "svelte/transition";

  let { section, children } = $props();

  const REDUCED_MOTION =
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const DURATION = REDUCED_MOTION ? 0 : 300;
</script>

{#key section}
  <div
    class="page-turn"
    in:fly={{ x: 12, duration: DURATION, opacity: 0 }}
    out:fly={{ x: -12, duration: DURATION, opacity: 0 }}
  >
    {@render children()}
  </div>
{/key}

<style>
  /* A transition needs a real box to apply transform/opacity to (display:
     contents would strip it out of the render tree entirely). Block + full
     width keeps it from otherwise reshaping the parent <main>'s layout. */
  .page-turn {
    display: block;
    width: 100%;
  }
</style>
