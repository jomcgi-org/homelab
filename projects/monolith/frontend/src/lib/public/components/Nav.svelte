<script>
  /** @type {{ route?: string, isPrivate?: boolean }} */
  let { route = "home", isPrivate = false } = $props();

  // NOTES, ENGINEERING, and CV are same-host relative URLs so they
  // resolve to jomcgi.dev/* from the public homepage and to
  // private.jomcgi.dev/* from the private dashboard, without
  // bouncing public visitors into the auth-gated private surface.
  // HOME always points at the public site.
  const publicItems = [
    { slug: "home", label: "HOME", href: "https://jomcgi.dev/" },
    { slug: "notes", label: "NOTES", href: "/notes" },
    { slug: "engineering", label: "ENGINEERING", href: "/engineering" },
    { slug: "cv", label: "CV", href: "/cv" },
  ];

  // REVIEW only renders on the private tier — the route exists only at
  // routes/private/review/ and showing the link on public.jomcgi.dev
  // would leak the existence of an internal surface.
  const privateItems = [
    { slug: "review", label: "REVIEW", href: "/review" },
  ];

  const items = $derived(
    isPrivate ? [...publicItems, ...privateItems] : publicItems,
  );

  // Interactive apps under /app/*. Relative hrefs so they resolve on
  // whichever tier the nav renders on (the apex/public. reroute rewrites
  // /app/* under /public/app/*). Add new apps here — the dropdown grows
  // automatically. The matching `slug` lets the APPS trigger underline
  // when an app page passes its own slug as `route`.
  const apps = [
    {
      slug: "hikes",
      label: "Hikes",
      desc: "Scottish hill-walk planner",
      href: "/app/hikes",
    },
    {
      slug: "ships",
      label: "Ships",
      desc: "Live AIS vessel tracker",
      href: "/app/ships",
    },
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
                    role="menuitem"
                    onclick={() => (open = false)}
                  >
                    <span class="md-apps-icon" aria-hidden="true">
                      {#if app.slug === "hikes"}
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
    box-shadow: 4px 4px 0 #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
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

  .md-apps-item:hover,
  .md-apps-item:focus-visible {
    background: #f3ede1; /* nosemgrep: svelte-hardcoded-color-in-style */
    border-color: #1a1a1a; /* nosemgrep: svelte-hardcoded-color-in-style */
    outline: none;
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
