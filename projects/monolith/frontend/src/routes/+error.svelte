<script>
  // Root error boundary. A "route not found" 404 never matches the /public or
  // /private route groups, so it surfaces here at the root, wrapped only by
  // src/routes/+layout.svelte. That root layout loads global.css (the legacy
  // mono token set) but NOT the brutalist design-system.css or its webfonts
  // (those live in public/+layout.svelte, which a 404 never reaches). So this
  // component pulls in the design system and fonts itself to render on-brand.
  import "$lib/public/styles/design-system.css";
  import { page } from "$app/stores";

  let status = $derived($page.status);
  let isNotFound = $derived(status === 404);

  // The browser path the visitor actually typed. $page.url reflects the
  // browser URL, not the hooks.js reroute target, so this shows "/asda"
  // rather than the internal "/public/asda".
  let path = $derived($page.url.pathname);

  let eyebrow = $derived(
    isNotFound
      ? `ERROR 404 // OFF THE MAP`
      : `ERROR ${status} // SOMETHING BROKE`,
  );
  let headline = $derived(
    isNotFound ? "you wandered off the map." : "the server hit a rough patch.",
  );
  let blurb = $derived(
    isNotFound
      ? "this page never existed, or it got refactored into the void. either way, there's nothing docked here."
      : ($page.error?.message ??
          "an unexpected error knocked this page over. it's not you, it's the cluster."),
  );
</script>

<svelte:head>
  <title>{status} · jomcgi.dev</title>
  <meta name="robots" content="noindex" />
  <link rel="icon" type="image/svg+xml" href="/favicon.svg" />
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link
    rel="preconnect"
    href="https://fonts.gstatic.com"
    crossorigin="anonymous"
  />
  <link
    href="https://fonts.googleapis.com/css2?family=Hanken+Grotesk:wght@300;400;500;600&family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600;700&display=swap"
    rel="stylesheet"
  />
</svelte:head>

<main class="nf">
  <!-- decorative brutalist shapes, same flat-ink language as the homepage hero -->
  <svg
    class="deco deco-star"
    width="56"
    height="56"
    viewBox="0 0 40 40"
    aria-hidden="true"
    ><path
      d="M20,2 L22.5,14 L34,10 L26,20 L34,30 L22.5,26 L20,38 L17.5,26 L6,30 L14,20 L6,10 L17.5,14 Z"
      fill="var(--blue)"
      stroke="var(--ink)"
      stroke-width="2"
      stroke-linejoin="round"
    /></svg
  >
  <svg
    class="deco deco-diamond"
    width="22"
    height="22"
    viewBox="0 0 24 24"
    aria-hidden="true"
    ><path
      d="M12,2 L22,12 L12,22 L2,12 Z"
      fill="none"
      stroke="var(--ink)"
      stroke-width="2"
    /></svg
  >
  <svg
    class="deco deco-circle"
    width="18"
    height="18"
    viewBox="0 0 24 24"
    aria-hidden="true"
    ><circle
      cx="12"
      cy="12"
      r="10"
      fill="var(--coral)"
      stroke="var(--ink)"
      stroke-width="2"
    /></svg
  >
  <svg
    class="deco deco-squiggle"
    width="80"
    height="24"
    viewBox="0 0 80 24"
    aria-hidden="true"
    ><path
      d="M2,12 Q 10,2 18,12 T 34,12 T 50,12 T 66,12 T 78,12"
      fill="none"
      stroke="var(--ink)"
      stroke-width="2.5"
      stroke-linecap="round"
    /></svg
  >

  <div class="nf-inner">
    <p class="eyebrow nf-eyebrow">{eyebrow}</p>

    <!-- The status number is the centerpiece. A dashed "route" runs across it
         and ends at an X, evoking a map track that leads nowhere. -->
    <div class="nf-code-wrap">
      <span class="display nf-code">{status}</span>
      <svg
        class="nf-track"
        viewBox="0 0 400 80"
        aria-hidden="true"
        preserveAspectRatio="none"
      >
        <path
          d="M8,64 Q 90,12 170,52 T 330,40"
          fill="none"
          stroke="var(--ink)"
          stroke-width="2.5"
          stroke-dasharray="2 10"
          stroke-linecap="round"
        />
        <path
          d="M322,32 L342,48 M342,32 L322,48"
          stroke="var(--coral)"
          stroke-width="3.5"
          stroke-linecap="round"
        />
      </svg>
    </div>

    <h1 class="display nf-headline">{headline}</h1>
    <p class="nf-blurb">{blurb}</p>

    {#if isNotFound}
      <p class="mono nf-path">
        you tried: <span class="nf-path-val">{path}</span>
      </p>
    {/if}

    <div class="nf-cta">
      <a href="https://jomcgi.dev/" class="btn btn-primary">
        <span class="btn-arr">←</span>&nbsp;BACK TO SOLID GROUND
      </a>
      <a href="/app/notes" class="btn btn-secondary">TALK TO MY NOTES</a>
    </div>
  </div>
</main>

<style>
  .nf {
    position: relative;
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--cream);
    color: var(--ink);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 48px 24px;
    overflow: hidden;
  }

  .nf-inner {
    position: relative;
    z-index: 1;
    max-width: 720px;
    text-align: center;
    animation: nf-in 480ms cubic-bezier(0.2, 0.7, 0.2, 1) both;
  }

  .nf-eyebrow {
    margin-bottom: 20px;
  }

  /* ── Giant status code ──────────────────── */
  .nf-code-wrap {
    position: relative;
    display: inline-block;
    margin-bottom: 8px;
  }

  .nf-code {
    display: block;
    font-size: clamp(140px, 30vw, 340px);
    line-height: 0.82;
    color: var(--ink);
    /* sit the number on a slab of accent yellow, brutalist highlight style */
    text-shadow: var(--shadow-hard-lg);
  }

  .nf-track {
    position: absolute;
    left: -4%;
    right: -4%;
    bottom: 14%;
    width: 108%;
    height: 40%;
    pointer-events: none;
  }

  .nf-headline {
    font-size: clamp(32px, 6vw, 56px);
    line-height: 1;
    margin: 12px 0 18px;
  }

  .nf-blurb {
    font-family: var(--sans);
    font-size: clamp(15px, 2.2vw, 18px);
    line-height: 1.55;
    color: var(--ink-3);
    max-width: 52ch;
    margin: 0 auto 14px;
  }

  .nf-path {
    font-size: 13px;
    color: var(--ink-3);
    margin-bottom: 32px;
  }

  .nf-path-val {
    color: var(--ink);
    background: var(--accent);
    padding: 2px 8px;
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard-sm);
    word-break: break-all;
  }

  .nf-cta {
    display: flex;
    gap: 14px;
    justify-content: center;
    flex-wrap: wrap;
  }

  .btn-arr {
    display: inline-block;
  }

  /* ── Decorative shapes ──────────────────── */
  .deco {
    position: absolute;
    z-index: 0;
    opacity: 0.9;
  }

  .deco-star {
    top: 12%;
    left: 14%;
    animation: nf-bob 6s ease-in-out infinite;
  }

  .deco-diamond {
    top: 22%;
    right: 16%;
    animation: nf-bob 7s ease-in-out infinite reverse;
  }

  .deco-circle {
    bottom: 20%;
    left: 20%;
    animation: nf-bob 5.5s ease-in-out infinite;
  }

  .deco-squiggle {
    bottom: 16%;
    right: 14%;
  }

  @keyframes nf-in {
    from {
      opacity: 0;
      transform: translateY(18px);
    }
    to {
      opacity: 1;
      transform: none;
    }
  }

  @keyframes nf-bob {
    0%,
    100% {
      transform: translateY(0) rotate(0deg);
    }
    50% {
      transform: translateY(-10px) rotate(6deg);
    }
  }

  @media (max-width: 640px) {
    /* shapes crowd the text on narrow screens; pull the closest two */
    .deco-star {
      left: 6%;
      top: 8%;
    }
    .deco-diamond {
      right: 7%;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .nf-inner,
    .deco {
      animation: none;
    }
  }
</style>
