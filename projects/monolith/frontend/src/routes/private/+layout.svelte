<script>
  import "$lib/private/dashboard-theme.css";
  import { page } from "$app/stores";
  import { periodForHour } from "$lib/private/period.js";

  let { children } = $props();
  let hour = $state(new Date().getHours());
  let period = $derived(periodForHour(hour));
  // The layout survives client-side navigation, so without a tick its
  // palette would freeze at first mount and drift from the page under it.
  $effect(() => {
    const id = setInterval(() => (hour = new Date().getHours()), 60_000);
    return () => clearInterval(id);
  });

  // Minimal private-tier chrome: the shared Nav is suppressed for the whole
  // tier (see routes/+layout.svelte), so every private page other than the
  // dashboard root gets a small fixed "back to dashboard" link.
  //
  // Suppressed on:
  // - the dashboard root itself ("/"): it IS the nav
  // - /app/*: gateway-routed full-screen apps (ArgoCD, Longhorn) with their
  //   own chrome
  // - /demos/*: renders its own Grimoire-style topbar (wordmark + tabs) that
  //   the link would collide with
  // - /review: renders its own top bar (tabs + mode toggle) flush with the
  //   top-left, where the link would sit on top of the tabs
  // - /chat and /notes: both render their own top-left chrome (the explorer
  //   header and the notes status bar) that the link would overlap
  //
  // $page.url reflects the browser URL (hooks.js reroute keeps private
  // paths un-prefixed), but strip a literal /private prefix too in case a
  // route is hit directly.
  let showBack = $derived.by(() => {
    const path = $page.url.pathname.replace(/^\/private(?=\/|$)/, "") || "/";
    if (path === "/") return false;
    if (/^\/(app|demos|review|chat|notes|agents)(\/|$)/.test(path))
      return false;
    return true;
  });
</script>

{#if showBack}
  <a class="back-to-dashboard shell {period}" href="/">&larr; dashboard</a>
{/if}

{@render children()}

<style>
  /* A quiet text link. This renders OUTSIDE any page's .shell, so it carries
     the current period itself. dashboard-theme.css's .shell class only defines
     custom properties, making the matching palette resolve here without
     inheriting any page styling. */
  .back-to-dashboard {
    position: fixed;
    top: 1.1rem;
    left: 1.2rem;
    z-index: 1000;
    font-family: var(--font-ui);
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0.01em;
    color: var(--ink-2);
    text-decoration: none;
    padding: 4px 2px;
    transition: color 0.15s ease;
  }

  .back-to-dashboard:hover {
    color: var(--accent);
  }

  .back-to-dashboard:focus-visible {
    outline: 1.5px solid var(--accent);
    outline-offset: 2px;
    border-radius: 4px;
  }
</style>
