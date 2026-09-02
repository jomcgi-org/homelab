<script>
  import { page } from "$app/state";
  import "$lib/public/styles/technical-drawing.css";

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

<nav class="td chrome trail" aria-label="You are here">
  <a class="crumb" href="/">jomcgi.dev</a>
  <a class="crumb" href="/blog" aria-current={post ? undefined : "page"}>blog</a
  >
  {#if post}
    <span class="crumb current" aria-current="page">{post}</span>
  {/if}
</nav>
<div class="td chrome chrome-right">
  <button
    class="chrome-link scheme"
    type="button"
    onclick={flip}
    aria-label={dark ? "Switch to day scheme" : "Switch to night scheme"}
    title={dark ? "Day" : "Night"}
  >
    {#if dark}
      <!-- sun -->
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
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
      <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
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
  .chrome {
    position: fixed;
    top: 1.1rem;
    left: 1.2rem;
    z-index: 1000;
    display: flex;
    gap: 0.5rem;
  }

  .trail {
    gap: 0;
    max-width: calc(100vw - 6rem);
    border: 1px solid var(--stroke);
    background: var(--sheet);
  }

  .crumb,
  .chrome-link {
    padding: 4px 6px;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    text-decoration: none;
    white-space: nowrap;
  }

  .crumb + .crumb {
    border-left: 1px solid var(--stroke);
  }

  .crumb.current {
    overflow: hidden;
    color: var(--ink);
    text-overflow: ellipsis;
  }

  .chrome-link {
    border: 1px solid var(--stroke);
    background: var(--sheet);
    cursor: pointer;
  }

  a.crumb:hover,
  .chrome-link:hover {
    color: var(--accent-ink);
  }

  .crumb:focus-visible,
  .chrome-link:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }

  .chrome-right {
    left: auto;
    right: 1.2rem;
  }

  button.chrome-link.scheme {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.9em;
    height: 1.9em;
    padding: 0;
  }

  .scheme svg {
    display: block;
  }
</style>
