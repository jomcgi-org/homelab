<script>
  import "$lib/public/styles/technical-drawing.css";

  let { children } = $props();

  // Scheme choice: "system" (no attribute), "light", or "dark", stamped on
  // the root as data-theme so technical-drawing.css can resolve it ahead
  // of prefers-color-scheme. Persisted per browser; the inline script in
  // <svelte:head> re-applies it before first paint so there is no flash.
  const KEY = "td-theme";
  const ORDER = ["system", "light", "dark"];
  let theme = $state("system");

  function apply(next) {
    theme = next;
    const root = document.documentElement;
    if (next === "system") delete root.dataset.theme;
    else root.dataset.theme = next;
    try {
      if (next === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch {
      // Storage can be unavailable (private mode); the choice then lasts
      // for this page view only.
    }
  }

  function cycle() {
    apply(ORDER[(ORDER.indexOf(theme) + 1) % ORDER.length]);
  }

  $effect(() => {
    const stored = document.documentElement.dataset.theme;
    if (stored === "light" || stored === "dark") theme = stored;
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
  <a class="chrome-link" href="/">&larr; jomcgi.dev</a>
  <button
    class="chrome-link"
    type="button"
    onclick={cycle}
    aria-label={`Colour scheme: ${theme}. Activate to change.`}
  >
    {theme === "system" ? "auto" : theme === "light" ? "day" : "night"}
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

  .chrome-link {
    padding: 4px 6px;
    border: 1px solid var(--stroke);
    background: var(--sheet);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
    font-weight: 500;
    letter-spacing: 0.02em;
    text-decoration: none;
    cursor: pointer;
  }

  .chrome-link:hover {
    color: var(--accent-ink);
  }

  .chrome-link:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  button.chrome-link {
    min-width: 3.4em;
    text-align: center;
  }
</style>
