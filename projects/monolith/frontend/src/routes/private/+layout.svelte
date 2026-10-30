<script>
  import "$lib/public/styles/technical-drawing.css";
  import { page } from "$app/stores";

  let { children } = $props();

  // Minimal private-tier chrome: the tier has no nav bar (the dashboard
  // root is the nav), so every private page other than the dashboard root
  // gets a small "back to dashboard" link.
  //
  // Suppressed on:
  // - the dashboard root itself ("/"): it IS the nav
  // - /app/*: gateway-routed full-screen apps (ArgoCD, Longhorn) with their
  //   own chrome
  // - /review: renders its own top bar (tabs + mode toggle) flush with the
  //   top-left, where the link would sit on top of the tabs
  // - /chat and /notes: both render their own top-left chrome (the explorer
  //   header and the notes status bar) that the link would overlap
  // - /qwen: its session list fills the top-left; the page links home itself
  //
  // $page.url reflects the browser URL (hooks.js reroute keeps private
  // paths un-prefixed), but strip a literal /private prefix too in case a
  // route is hit directly.
  let path = $derived(
    $page.url.pathname.replace(/^\/private(?=\/|$)/, "") || "/",
  );
  let executionBack = $derived(/^\/factory\/execution(\/|$)/.test(path));
  let showBack = $derived.by(() => {
    if (path === "/") return false;
    if (/^\/(app|review|chat|notes|qwen)(\/|$)/.test(path)) return false;
    // These factory documents carry their own Trail and persisted scheme
    // switch. Execution routes still need the legacy dashboard link.
    if (/^\/factory(?:\/?|\/escalations\/?|\/work-items\/[^/]+\/?)$/.test(path))
      return false;
    return true;
  });
</script>

<svelte:head>
  <!-- The back link can be the only sheet surface on a fresh child load.
       Restore the same preference as SchemeToggle before it is painted. -->
  <script>
    try {
      var t = localStorage.getItem("td-theme");
      if (t === "light" || t === "dark")
        document.documentElement.dataset.theme = t;
    } catch (e) {}
  </script>
</svelte:head>

{#snippet backLink()}
  <a class="back-to-dashboard td-chrome" href="/">&larr; dashboard</a>
{/snippet}

{#if executionBack}
  <!-- Execution and drain own the top-left of their headers. Reserve a row
       instead of laying the opaque dashboard link over their navigation. -->
  <div class="private-execution">
    <nav class="back-row td-chrome" aria-label="Dashboard">
      {@render backLink()}
    </nav>
    {@render children()}
  </div>
{:else}
  {#if showBack}{@render backLink()}{/if}
  {@render children()}
{/if}

<style>
  .private-execution {
    --private-back-height: 48px;
  }

  .back-row {
    height: var(--private-back-height);
    display: flex;
    align-items: center;
    padding: 0 1.2rem;
    background: var(--sheet);
  }

  .back-row .back-to-dashboard {
    position: static;
  }

  /* Keep the full-screen console, including its visual-viewport override,
     inside the space below the reserved row. Drain remains scrollable. */
  .private-execution :global(main.console) {
    height: calc(var(--console-h, 100dvh) - var(--private-back-height));
  }

  /* Token-only chrome must not match body:has(.td): full-screen children
     own their scroll containers. The shared palette follows data-theme. */
  .back-to-dashboard {
    position: fixed;
    top: 8px;
    left: 1.2rem;
    z-index: 1000;
    font-family: var(--font-code);
    font-size: 13px;
    line-height: 1.3;
    font-weight: 500;
    letter-spacing: 0.01em;
    color: var(--ink-2);
    text-decoration: none;
    padding: 6px 8px;
    border: 1px solid var(--stroke);
    background: var(--sheet);
  }

  .back-to-dashboard:hover {
    color: var(--accent-ink);
  }

  .back-to-dashboard:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 2px;
  }
</style>
