<script>
  import { page } from "$app/stores";

  let { children } = $props();

  // Minimal private-tier chrome: the shared Nav is suppressed for the whole
  // tier (see routes/+layout.svelte), so every private page other than the
  // dashboard root gets a small fixed "back to dashboard" link.
  //
  // Suppressed on:
  // - the dashboard root itself ("/"): it IS the nav
  // - /app/*: gateway-routed full-screen apps (SigNoz, ArgoCD) with their
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
    const path =
      $page.url.pathname.replace(/^\/private(?=\/|$)/, "") || "/";
    if (path === "/") return false;
    if (/^\/(app|demos|review|chat|notes)(\/|$)/.test(path)) return false;
    return true;
  });
</script>

{#if showBack}
  <a class="back-to-dashboard" href="/">&larr; dashboard</a>
{/if}

{@render children()}

<style>
  /* A quiet pill. Variables use fallbacks so it reads correctly on both the
     dashboard theme (--ink-*, --font-ui, --accent, --card-bg, --line) and the
     older brutalist theme (--fg-*, --font). */
  .back-to-dashboard {
    position: fixed;
    top: 0.9rem;
    left: 1.1rem;
    z-index: 1000;
    font-family: var(--font-ui, var(--font));
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.03em;
    color: var(--ink-2, var(--fg-secondary));
    background: var(--card-bg, transparent);
    border: 1px solid var(--line, var(--fg-tertiary));
    border-radius: 999px;
    padding: 4px 12px;
    text-decoration: none;
    transition:
      color 0.15s ease,
      border-color 0.15s ease;
  }

  .back-to-dashboard:hover {
    color: var(--accent, var(--fg));
    border-color: var(--accent, var(--fg));
  }

  .back-to-dashboard:focus-visible {
    outline: 1.5px solid var(--accent, var(--fg));
    outline-offset: 2px;
  }
</style>
