<script>
  import "@homelab/design-system/tokens/contract.css";
  import "$lib/global.css";
  import { page } from "$app/stores";
  import { Nav } from "$lib/public/components";

  let { children } = $props();

  let isPrivate = $derived($page.url.hostname.startsWith("private."));
  let isFriends = $derived($page.url.hostname.startsWith("friends."));

  // The private tier drops the shared Nav entirely: the dashboard at the
  // private root IS the nav (launcher grid, queue links, back affordances),
  // and routes/private/+layout.svelte renders its own minimal chrome (a
  // small "back to dashboard" link on non-root private paths).
  //
  // Apps under /app/* are full-screen experiences (e.g. the live ships map)
  // that render their own chrome, so the site nav is suppressed for them. The
  // hooks.js reroute keeps the browser path un-prefixed, but match the
  // /public|/private prefixes too in case a route is hit directly.
  //
  // Docs under /docs are likewise suppressed: DocsShell renders its own
  // purpose-built topbar (back-to-apex link, docs search, repository link), so the
  // global site nav would just stack a second sticky bar on top of it. Matches
  // /docs and /docs/* but not unrelated prefixes like /docstore.
  // Blog pages use their own technical drawing chrome, matching the docs
  // suppression for /blog and /blog/*.
  //
  // Error pages (notably the brutalist 404 in src/routes/+error.svelte) also
  // suppress the nav: a not-found page renders its own "back home" affordance
  // and the cross-tier nav would only clutter the dead-end.
  //
  // Artifacts under /artifact/* (ADR 024 goosecracker) are full-bleed sandboxed
  // pages: the page is a 100vh iframe with body overflow hidden, meant to fill
  // the viewport. The site nav both looks wrong on a standalone artifact and
  // pushes the iframe down, so suppress it like the /app/* experiences.
  //
  // Firecracker demos under /demos/* render their own Grimoire-style topbar
  // (wordmark + tabs) and are a full-page tool, not a page of the portfolio
  // site, so the global nav is suppressed here too.
  //
  // The /ember/* pages are their own small site in the fcstory visual
  // language, each with its own wordmark topbar linking home; they had this
  // suppression under the old /app/firecracker path and lost it in the move
  // to /ember.
  let hideNav = $derived(
    isPrivate ||
      isFriends ||
      /^\/private(?:\/|$)/.test($page.url.pathname) ||
      /^\/(public\/|private\/)?app\//.test($page.url.pathname) ||
      /^\/(public\/|private\/)?docs(\/|$)/.test($page.url.pathname) ||
      /^\/(public\/|private\/)?blog(\/|$)/.test($page.url.pathname) ||
      /^\/(public\/|private\/)?artifact(\/|$)/.test($page.url.pathname) ||
      /^\/(public\/|private\/)?demos(\/|$)/.test($page.url.pathname) ||
      /^\/(public\/|private\/)?ember(\/|$)/.test($page.url.pathname) ||
      $page.error != null,
  );

  // Active-state derivation. The hooks.js reroute remaps
  // public.jomcgi.dev/* → /public/* and private.jomcgi.dev/* → /private/*
  // internally, but $page.url reflects the *browser* URL. So:
  // - /review (private host) → "review"
  // - /engineering (any host) → "engineering"
  // - /cv (any host) → "cv"
  // - any other URL on public.jomcgi.dev → "home"
  // - everything else → no active state
  // Notes lives under /app/notes: like the other /app/* apps it is full-screen
  // and renders no site nav at all (the global nav is suppressed above), so it
  // needs no active-state detection here.
  let activeRoute = $derived.by(() => {
    const host = $page.url.hostname;
    const path = $page.url.pathname;
    if (path === "/review" || path.startsWith("/review/")) return "review";
    if (path === "/engineering" || path.startsWith("/engineering/")) {
      return "engineering";
    }
    if (path === "/cv" || path.startsWith("/cv/")) return "cv";
    if (host.startsWith("public.")) return "home";
    return "";
  });
</script>

{#if !hideNav}
  <Nav route={activeRoute} {isPrivate} />
{/if}

{@render children()}
