<script>
  import "$lib/public/styles/technical-drawing.css";
  import SchemePreference from "$lib/public/components/SchemePreference.svelte";
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
  // - /updates: carries its own Trail and scheme switch, like the factory
  //   documents
  //
  // $page.url reflects the browser URL (hooks.js reroute keeps private
  // paths un-prefixed), but strip a literal /private prefix too in case a
  // route is hit directly.
  let path = $derived(
    $page.url.pathname.replace(/^\/private(?=\/|$)/, "") || "/",
  );
  let showBack = $derived.by(() => {
    if (path === "/") return false;
    if (/^\/(app|review|chat|notes|qwen)(\/|$)/.test(path)) return false;
    // The factory documents, the execution console and the updates journal
    // carry their own Trail, whose root is the dashboard, and their own
    // persisted scheme switch.
    if (
      /^\/factory(?:\/?|\/escalations\/?|\/work-items\/[^/]+\/?|\/execution(?:\/.*)?)$/.test(
        path,
      )
    )
      return false;
    if (/^\/updates\/?$/.test(path)) return false;
    return true;
  });
</script>

<!-- The back link can be the only sheet surface on a fresh child load.
     The shared bootstrap restores the stored scheme before it is painted. -->
<SchemePreference />

{#snippet backLink()}
  <a class="back-to-dashboard td-chrome" href="/">&larr; dashboard</a>
{/snippet}

{#if showBack}{@render backLink()}{/if}
{@render children()}

<style>
  /* Token-only chrome must not match body:has(.td): full-screen children
     own their scroll containers. The shared palette follows data-theme. */
  .back-to-dashboard {
    position: fixed;
    top: 8px;
    left: 1.2rem;
    z-index: 1000;
    display: inline-flex;
    align-items: center;
    /* The one control a child page is guaranteed to have: a thumb target. */
    min-height: 44px;
    font-family: var(--font-code);
    font-size: 13px;
    line-height: 1.3;
    font-weight: 500;
    letter-spacing: 0.01em;
    color: var(--ink-2);
    text-decoration: none;
    padding: 6px 12px;
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
