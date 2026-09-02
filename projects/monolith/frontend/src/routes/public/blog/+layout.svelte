<script>
  import { page } from "$app/state";
  import "$lib/public/styles/technical-drawing.css";
  import Trail from "./Trail.svelte";

  let { children } = $props();

  // The trail is one box partitioned by rules, the same way a figure's
  // parts are: site, then the blog, then the post being read.
  const post = $derived(page.params.slug ? page.data.title : "");

  // Scheme: the system preference by default (no attribute stored), or an
  // explicit "light" / "dark" stamped on the root as data-theme once the
  // reader clicks the toggle. technical-drawing.css resolves data-theme
  // ahead of prefers-color-scheme. Persisted per browser; the inline script
  // in <svelte:head> re-applies it before first paint so there is no flash.
  const KEY = "td-theme";
  // The scheme currently painted, whichever source decided it.
  let dark = $state(false);

  function apply(next) {
    const root = document.documentElement;
    root.dataset.theme = next;
    dark = next === "dark";
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Storage can be unavailable (private mode); the choice then lasts
      // for this page view only.
    }
  }

  function flip() {
    apply(dark ? "light" : "dark");
  }

  $effect(() => {
    const stored = document.documentElement.dataset.theme;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const sync = () => {
      const t = document.documentElement.dataset.theme;
      dark = t === "dark" || (t !== "light" && media.matches);
    };
    sync();
    if (stored !== "light" && stored !== "dark") {
      media.addEventListener("change", sync);
      return () => media.removeEventListener("change", sync);
    }
  });
</script>

<svelte:head>
  <!-- Applied before paint; mirrors apply() above. -->
  <script>
    try {
      var t = localStorage.getItem("td-theme");
      if (t === "light" || t === "dark")
        document.documentElement.dataset.theme = t;
    } catch (e) {}
  </script>
</svelte:head>

<div class="td chrome">
  <div class="trail-slot">
    <Trail {post} />
  </div>
  <button
    class="scheme"
    type="button"
    onclick={flip}
    aria-label={dark ? "Switch to day scheme" : "Switch to night scheme"}
    title={dark ? "Day" : "Night"}
  >
    {#if dark}
      <!-- sun -->
      <svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true">
        <circle
          cx="8"
          cy="8"
          r="3.2"
          fill="none"
          stroke="currentColor"
          stroke-width="1.25"
        />
        <g stroke="currentColor" stroke-width="1.25" stroke-linecap="round">
          <path
            d="M8 1v2M8 13v2M1 8h2M13 8h2M3.1 3.1l1.4 1.4M11.5 11.5l1.4 1.4M3.1 12.9l1.4-1.4M11.5 4.5l1.4-1.4"
          />
        </g>
      </svg>
    {:else}
      <!-- moon -->
      <svg viewBox="0 0 16 16" width="18" height="18" aria-hidden="true">
        <path
          d="M10.5 2.2A6 6 0 1 0 13.8 9.5 5 5 0 0 1 10.5 2.2z"
          fill="none"
          stroke="currentColor"
          stroke-width="1.25"
          stroke-linejoin="round"
        />
      </svg>
    {/if}
  </button>
</div>

{@render children()}

<style>
  /* On a wide screen only the scheme switch floats, top right; the trail
     is docked at the top of the index column by the pages themselves so
     it lines up with the index and sticks with it. On a phone there is
     no index column to dock in, so the trail joins the switch in one row
     in the flow at the top of the page and scrolls away with it. */
  .chrome {
    position: fixed;
    top: 1.1rem;
    right: 1.2rem;
    z-index: 1000;
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
  }

  .trail-slot {
    display: none;
    min-width: 0;
  }

  .scheme {
    display: inline-flex;
    flex: none;
    align-items: center;
    justify-content: center;
    width: 2.25rem;
    height: 2.25rem;
    padding: 0;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink-2);
    cursor: pointer;
  }

  .scheme:hover {
    color: var(--accent-ink);
  }

  .scheme svg {
    display: block;
  }

  .scheme:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }

  @media (max-width: 760px) {
    .chrome {
      position: static;
      padding: 1rem 1em 0;
      background: var(--sheet);
    }

    .trail-slot {
      display: block;
    }
  }
</style>
