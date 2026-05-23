<script>
  /** @type {{ route?: string }} */
  let { route = "home" } = $props();

  // NOTES is a same-host relative URL so it resolves to
  // public.jomcgi.dev/notes from the public homepage and to
  // private.jomcgi.dev/notes from the private dashboard, without
  // bouncing public visitors into the auth-gated private surface.
  // Other links are cross-host by design — HOME always points at the
  // public site, ENGINEERING and CV live on the apex domain.
  const items = [
    { slug: "home", label: "HOME", href: "https://public.jomcgi.dev/" },
    { slug: "notes", label: "NOTES", href: "/notes" },
    {
      slug: "engineering",
      label: "ENGINEERING",
      href: "https://jomcgi.dev/engineering/",
    },
    { slug: "cv", label: "CV", href: "https://jomcgi.dev/cv/" },
  ];
</script>

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
    background: #ffffff;
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
    color: #2a2824;
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
    background: #ff7169;
    transform: scaleX(0);
    transition: transform 160ms ease;
  }

  .md-nav-link:hover {
    color: #1a1a1a;
  }

  .md-nav-link:hover::after {
    transform: scaleX(1);
  }

  .md-nav-link.active {
    color: #1a1a1a;
  }

  .md-nav-link.active::after {
    background: #1a1a1a;
    transform: scaleX(1);
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
  }
</style>
