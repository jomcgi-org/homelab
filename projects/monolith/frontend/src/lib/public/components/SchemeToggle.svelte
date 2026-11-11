<script>
  import SchemePreference from "./SchemePreference.svelte";
  // Scheme: the system preference by default (no attribute stored), or an
  // explicit "light" / "dark" stamped on the root as data-theme once the
  // reader clicks the toggle. technical-drawing.css resolves data-theme
  // ahead of prefers-color-scheme. Persisted per browser; the inline script
  // in <svelte:head> re-applies it before first paint so there is no flash.
  const KEY = "td-theme";
  // The scheme currently painted, whichever source decided it.
  let dark = $state(false);

  // The switch sits on a .td surface, so once the scheme is stamped the
  // sheet colour the page now paints is readable from it; the theme-color
  // metas in app.html take that value so the browser's own chrome follows
  // a hand-picked scheme rather than the OS one.
  let button = $state(null);

  function apply(next) {
    const root = document.documentElement;
    root.dataset.theme = next;
    dark = next === "dark";
    const sheet = button
      ? getComputedStyle(button).getPropertyValue("--sheet").trim()
      : "";
    if (sheet)
      for (const meta of document.querySelectorAll('meta[name="theme-color"]'))
        meta.setAttribute("content", sheet);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Storage can be unavailable (private mode); the choice then lasts
      // for this page view only.
    }
  }

  // Cross-fade the whole page between schemes where the browser can,
  // instead of every surface snapping at once. Reduced motion snaps.
  // `want` is the scheme a pending transition will apply, so a second
  // click before it runs flips from that rather than the painted scheme.
  let want = null;
  function flip() {
    const from = want ?? (dark ? "dark" : "light");
    const next = from === "dark" ? "light" : "dark";
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!document.startViewTransition || still) {
      apply(next);
      return;
    }
    want = next;
    document.startViewTransition(() => {
      apply(next);
      if (want === next) want = null;
    });
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

<SchemePreference />

<button
  bind:this={button}
  class="scheme"
  type="button"
  onclick={flip}
  aria-label={dark ? "Switch to day scheme" : "Switch to night scheme"}
  title={dark ? "Day" : "Night"}
>
  <!-- sun -->
  <svg
    class="glyph sun"
    viewBox="0 0 16 16"
    width="18"
    height="18"
    aria-hidden="true"
  >
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
  <!-- moon -->
  <svg
    class="glyph moon"
    viewBox="0 0 16 16"
    width="18"
    height="18"
    aria-hidden="true"
  >
    <path
      d="M10.5 2.2A6 6 0 1 0 13.8 9.5 5 5 0 0 1 10.5 2.2z"
      fill="none"
      stroke="currentColor"
      stroke-width="1.25"
      stroke-linejoin="round"
    />
  </svg>
</button>

<style>
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

  /* Both glyphs are in the markup and the scheme picks one in CSS, so
     the server-rendered icon is already right and nothing swaps after
     hydration. Same three-way resolution as technical-drawing.css. */
  .scheme .glyph {
    display: none;
  }

  .scheme .moon {
    display: block;
  }

  :global(:root[data-theme="dark"]) .scheme .moon {
    display: none;
  }

  :global(:root[data-theme="dark"]) .scheme .sun {
    display: block;
  }

  @media (prefers-color-scheme: dark) {
    :global(:root:not([data-theme="light"])) .scheme .moon {
      display: none;
    }

    :global(:root:not([data-theme="light"])) .scheme .sun {
      display: block;
    }
  }

  .scheme:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: -2px;
  }

  /* A thumb needs the 44px floor; the glyph stays the same size. */
  @media (max-width: 760px) {
    .scheme {
      width: 2.75rem;
      height: 2.75rem;
    }
  }
</style>
