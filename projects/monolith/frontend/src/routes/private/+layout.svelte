<script>
  import "$lib/public/styles/technical-drawing.css";
  import { page } from "$app/stores";

  let { children } = $props();

  // Minimal private-tier chrome: the shared Nav is suppressed for the whole
  // tier (see routes/+layout.svelte), so every private page other than the
  // dashboard root gets a small fixed "back to dashboard" link.
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
  let showBack = $derived.by(() => {
    const path = $page.url.pathname.replace(/^\/private(?=\/|$)/, "") || "/";
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

{#if showBack}
  <a class="back-to-dashboard td-chrome" href="/">&larr; dashboard</a>
{/if}

{@render children()}

<style>
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
