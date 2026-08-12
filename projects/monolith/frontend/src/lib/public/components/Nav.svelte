<script>
  import { apps as appsRegistry } from "$lib/public/apps.js";

  /** @type {{ route?: string, isPrivate?: boolean }} */
  let { route = "home", isPrivate = false } = $props();

  // ENGINEERING, DOCS, and CV are same-host relative URLs so they resolve to
  // jomcgi.dev/* from the public homepage and to private.jomcgi.dev/* from the
  // private dashboard, without bouncing public visitors into the auth-gated
  // private surface. HOME always points at the public site. Notes is no longer
  // a top-level link: it moved under the APPS dropdown alongside the other apps.
  // DOCS is reference material (repo docs + ADRs), a peer of ENGINEERING, not an
  // interactive app, so it sits in the top row rather than the APPS dropdown.
  const publicItems = [
    { slug: "home", label: "HOME", href: "/" },
    { slug: "engineering", label: "ENGINEERING", href: "/engineering" },
    { slug: "docs", label: "DOCS", href: "/docs" },
    { slug: "cv", label: "CV", href: "/cv" },
  ];

  // REVIEW only renders on the private tier — the route exists only at
  // routes/private/review/ and showing the link on public.jomcgi.dev
  // would leak the existence of an internal surface.
  const privateItems = [{ slug: "review", label: "REVIEW", href: "/review" }];

  const items = $derived(
    isPrivate ? [...publicItems, ...privateItems] : publicItems,
  );

  // Interactive apps under /app/*, from the shared registry
  // ($lib/public/apps.js) so this dropdown and the homepage rack never
  // drift out of sync. Featured apps (Grimoire, Firecracker) render first
  // since they're the flagship pieces; the rest keep the registry's order.
  // The matching `slug` lets the APPS trigger underline when an app page
  // passes its own slug as `route`.
  const apps = [
    ...appsRegistry.filter((a) => a.featured),
    ...appsRegistry.filter((a) => !a.featured),
  ];

  const appsActive = $derived(apps.some((a) => a.slug === route));

  let open = $state(false);
  /** @type {HTMLElement | undefined} */
  let appsEl = $state();

  function toggle() {
    open = !open;
  }

  /** @param {MouseEvent} e */
  function onWindowClick(e) {
    if (open && appsEl && !appsEl.contains(/** @type {Node} */ (e.target))) {
      open = false;
    }
  }

  /** @param {KeyboardEvent} e */
  function onWindowKeydown(e) {
    if (e.key === "Escape") open = false;
  }
</script>

<svelte:window onclick={onWindowClick} onkeydown={onWindowKeydown} />

<svelte:head>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin="anonymous"
  />
  <link
    href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<nav class="md-nav">
  <div class="md-nav-inner">
    <div class="md-nav-links">
      {#each items as item}
        <a
          href={item.href}
          class="md-nav-link"
          class:active={route === item.slug}
        >
          {item.label}
        </a>
        {#if item.slug === "engineering"}
          <div class="md-apps" bind:this={appsEl}>
            <button
              type="button"
              class="md-nav-link md-apps-trigger"
              class:active={appsActive}
              aria-haspopup="menu"
              aria-expanded={open}
              onclick={toggle}
            >
              APPS
              <svg
                class="md-apps-chevron"
                class:open
                width="9"
                height="9"
                viewBox="0 0 10 10"
                aria-hidden="true"
              >
                <path
                  d="M1 3 L5 7 L9 3"
                  fill="none"
                  stroke="currentColor"
                  stroke-width="1.6"
                  stroke-linecap="square"
                />
              </svg>
            </button>

            {#if open}
              <div class="md-apps-panel" role="menu">
                {#each apps as app}
                  <a
                    href={app.href}
                    class="md-apps-item"
                    data-app={app.slug}
                    role="menuitem"
                    onclick={() => (open = false)}
                  >
                    <span class="md-apps-icon" aria-hidden="true">
                      {#if app.slug === "grimoire"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <path
                            d="M12 5 C 9.5 3.5 6 3.5 3 4.5 V19 C 6 18 9.5 18 12 19.5 C 14.5 18 18 18 21 19 V4.5 C 18 3.5 14.5 3.5 12 5 Z"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linejoin="round"
                          />
                          <path
                            d="M12 5 V19.5"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                          />
                        </svg>
                      {:else if app.slug === "firecracker"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <rect
                            x="4"
                            y="7"
                            width="16"
                            height="12"
                            rx="1.5"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                          />
                          <path
                            d="M12 3 C 10 5.5 10 7 12 8.5 C 14 7 14 5.5 12 3 Z"
                            fill="currentColor"
                            stroke="none"
                          />
                          <path
                            d="M8 12 H16 M8 15 H16"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linecap="round"
                          />
                        </svg>
                      {:else if app.slug === "trips"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <path
                            d="M5 20 C 7 14 12 14 17 12"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linecap="round"
                            stroke-dasharray="0.2 3.4"
                          />
                          <circle
                            cx="5"
                            cy="20"
                            r="1.9"
                            fill="currentColor"
                            stroke="none"
                          />
                          <path
                            d="M17 3 C 14.5 3 13 4.8 13 6.8 C 13 9.6 17 12.5 17 12.5 C 17 12.5 21 9.6 21 6.8 C 21 4.8 19.5 3 17 3 Z"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linejoin="round"
                          />
                          <circle
                            cx="17"
                            cy="6.8"
                            r="1.4"
                            fill="currentColor"
                            stroke="none"
                          />
                        </svg>
                      {:else if app.slug === "hikes"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <path
                            d="M2 20 L9 6 L13 13 L16 8 L22 20 Z"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linejoin="round"
                          />
                          <path
                            d="M9 6 L11 10 L7 11 Z"
                            fill="currentColor"
                            stroke="none"
                          />
                        </svg>
                      {:else if app.slug === "ships"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <path
                            d="M3 14 H21 L18 20 H6 Z"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linejoin="round"
                          />
                          <path
                            d="M12 3 V14 M12 5 L18 11"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linecap="round"
                            stroke-linejoin="round"
                          />
                        </svg>
                      {:else if app.slug === "stars"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <path
                            d="M12 2 L14 9 L21 11 L14 13 L12 20 L10 13 L3 11 L10 9 Z"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linejoin="round"
                          />
                          <path
                            d="M18.5 3 L19 5 L21 5.5 L19 6 L18.5 8 L18 6 L16 5.5 L18 5 Z"
                            fill="currentColor"
                            stroke="none"
                          />
                        </svg>
                      {:else if app.slug === "notes"}
                        <svg width="22" height="22" viewBox="0 0 24 24">
                          <path
                            d="M4 19 V5 A1 1 0 0 1 5 4 H17 L20 7 V19 A1 1 0 0 1 19 20 H5 A1 1 0 0 1 4 19 Z"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linejoin="round"
                          />
                          <path
                            d="M7 9 H15 M7 12 H15 M7 15 H12"
                            fill="none"
                            stroke="currentColor"
                            stroke-width="1.8"
                            stroke-linecap="round"
                          />
                        </svg>
                      {/if}
                    </span>
                    <span class="md-apps-text">
                      <span class="md-apps-title">{app.label}</span>
                      <span class="md-apps-desc">{app.desc}</span>
                    </span>
                  </a>
                {/each}
              </div>
            {/if}
          </div>
        {/if}
      {/each}
    </div>
  </div>
</nav>

<style>
  /* Nav is shared 1:1 across tiers (public.jomcgi.dev + private.jomcgi.dev).
     Colours and font are hardcoded — not theme-able — so the component
     looks identical regardless of which tier's design tokens are loaded. */
  .md-nav {
    position: sticky;
    top: 0;
    z-index: 50;
    background: #ffffff; /* nosemgrep: svelte-hardcoded-color-in-style */
    border-bottom: 2px solid #1a1a1a;
  }

  .md-nav-inner {
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    padding: 14px 32px;
    max-width: 1360px;
    margin: 0 auto;
  }

  .md-nav-links {
    grid-column: 2;
    display: flex;
    gap: 4px;
    justify-self: center;
    align-items: center;
  }

  .md-nav-link {
    padding: 8px 12px;
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.1em;
    color: #2a2824; /* nosemgrep: svelte-hardcoded-color-in-style */
    text-decoration: none;
    transition: color 160ms ease;
    position: relative;
  }

  .md-nav-link::after {
    content: "";
    position: absolute;
    left: 12px;
    right: 12px;
    bottom: 2px;
    height: 2px;
    background: #ff7169; /* nosemgrep: svelte-hardcoded-color-in-style */
    transform: scaleX(0);
    transition: transform 160ms ease;
  }

  .md-nav-link:hover {
    color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-nav-link:hover::after {
    transform: scaleX(1);
  }

  .md-nav-link.active {
    color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-nav-link.active::after {
    background: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    transform: scaleX(1);
  }

  /* --- APPS dropdown ------------------------------------------------ */

  .md-apps {
    position: relative;
    display: flex;
    align-items: center;
  }

  /* The trigger is a <button> styled to read as a nav link. Reset the
     native button chrome so it inherits the .md-nav-link look. The
     ::after underline is shared with .md-nav-link, so hover/active
     behave identically to the anchor links. */
  .md-apps-trigger {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    background: none;
    border: none;
    cursor: pointer;
  }

  .md-apps-chevron {
    transition: transform 160ms ease;
  }

  .md-apps-chevron.open {
    transform: rotate(180deg);
  }

  .md-apps-panel {
    position: absolute;
    top: calc(100% + 8px);
    left: 50%;
    transform: translateX(-50%);
    z-index: 60;
    min-width: 268px;
    padding: 8px;
    background: #ffffff; /* nosemgrep: svelte-hardcoded-color-in-style */
    border: 2px solid #1a1a1a;
    animation: md-apps-in 120ms ease;
  }

  @keyframes md-apps-in {
    from {
      opacity: 0;
      transform: translateX(-50%) translateY(-4px);
    }
    to {
      opacity: 1;
      transform: translateX(-50%) translateY(0);
    }
  }

  .md-apps-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px;
    text-decoration: none;
    border: 2px solid transparent;
    transition:
      background 120ms ease,
      border-color 120ms ease;
  }

  /* On hover/focus the row tints with the app's accent and the icon tile
     fills with the full accent; the ink border + ink icon stroke stay for
     brutalist contrast. Accents are keyed off the data-app attribute so
     each app highlights in its own colour (Hikes green, Ships blue). */
  .md-apps-item:hover,
  .md-apps-item:focus-visible {
    border-color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    outline: none;
  }

  .md-apps-item[data-app="grimoire"]:hover,
  .md-apps-item[data-app="grimoire"]:focus-visible {
    background: #f4ecff; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="grimoire"]:hover .md-apps-icon,
  .md-apps-item[data-app="grimoire"]:focus-visible .md-apps-icon {
    background: #b14fff; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-item[data-app="firecracker"]:hover,
  .md-apps-item[data-app="firecracker"]:focus-visible {
    background: #ffece9; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="firecracker"]:hover .md-apps-icon,
  .md-apps-item[data-app="firecracker"]:focus-visible .md-apps-icon {
    background: #ff8d7a; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-item[data-app="hikes"]:hover,
  .md-apps-item[data-app="hikes"]:focus-visible {
    background: #e8fbef; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="hikes"]:hover .md-apps-icon,
  .md-apps-item[data-app="hikes"]:focus-visible .md-apps-icon {
    background: #4ade80; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-item[data-app="ships"]:hover,
  .md-apps-item[data-app="ships"]:focus-visible {
    background: #e7f4ff; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="ships"]:hover .md-apps-icon,
  .md-apps-item[data-app="ships"]:focus-visible .md-apps-icon {
    background: #6fc2ff; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-item[data-app="stars"]:hover,
  .md-apps-item[data-app="stars"]:focus-visible {
    background: #f4ecff; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="stars"]:hover .md-apps-icon,
  .md-apps-item[data-app="stars"]:focus-visible .md-apps-icon {
    background: #b14fff; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-item[data-app="notes"]:hover,
  .md-apps-item[data-app="notes"]:focus-visible {
    background: #fff6da; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="notes"]:hover .md-apps-icon,
  .md-apps-item[data-app="notes"]:focus-visible .md-apps-icon {
    background: #ffd84d; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-item[data-app="trips"]:hover,
  .md-apps-item[data-app="trips"]:focus-visible {
    background: #ffece9; /* nosemgrep: svelte-hardcoded-color-in-style */
  }
  .md-apps-item[data-app="trips"]:hover .md-apps-icon,
  .md-apps-item[data-app="trips"]:focus-visible .md-apps-icon {
    background: #ff8d7a; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-icon {
    flex: 0 0 auto;
    display: grid;
    place-items: center;
    width: 40px;
    height: 40px;
    color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    background: #ffffff; /* nosemgrep: svelte-hardcoded-color-in-style */
    border: 2px solid #1a1a1a;
    transition: background 120ms ease;
  }

  .md-apps-text {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .md-apps-title {
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  .md-apps-desc {
    font-family: "JetBrains Mono", ui-monospace, "SF Mono", monospace;
    font-size: 11px;
    font-weight: 400;
    color: #6b6658; /* nosemgrep: svelte-hardcoded-color-in-style */
  }

  @media (max-width: 768px) {
    .md-nav-inner {
      gap: 12px;
      padding: 10px 16px;
    }
    .md-nav-links {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      scrollbar-width: none;
      gap: 0;
    }
    .md-nav-links::-webkit-scrollbar {
      display: none;
    }
    .md-nav-link {
      padding: 6px 8px;
      font-size: 10px;
      white-space: nowrap;
    }
    /* On mobile the links row is overflow-x:auto, which also clips the
       cross axis — an absolutely-positioned panel would be cut off. Pin
       the panel to the viewport instead so it renders as a full-width
       sheet below the nav, escaping the scroll container entirely. */
    .md-apps-panel {
      position: fixed;
      top: 48px;
      left: 12px;
      right: 12px;
      transform: none;
      min-width: 0;
      animation: md-apps-in-mobile 120ms ease;
    }
    @keyframes md-apps-in-mobile {
      from {
        opacity: 0;
        transform: translateY(-4px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
  }
</style>
