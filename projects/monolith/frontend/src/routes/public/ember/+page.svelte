<script>
  // /ember: the landing page for the Ember mini-site. Minimalist by design:
  // one claim, one lede, two cards. The Postgres card carries a live state
  // line seeded by the SSR load (cached reads, never wakes the VM); the page
  // itself never polls. Visual language is the shared ember token sheet
  // (lib/public/ember/ember.css), same as /ember/postgres and
  // /ember/firecracker.
  import "$lib/public/ember/ember.css";

  let { data } = $props();

  // Same vocabulary as EmberStage's STATE_WORD so the landing page and the
  // demo never disagree about what the VM is doing.
  const STATE_WORD = {
    banked: "asleep",
    checkpointed: "asleep",
    banking: "falling asleep",
    relighting: "waking",
    cold_booting: "waking",
    starting: "waking",
    serving: "awake",
  };

  let stateWord = $derived(STATE_WORD[data.status?.state ?? ""] ?? null);
  let awake = $derived(stateWord === "awake" || stateWord === "waking");

  // Mirrors EmberStage.gbHours: raw MiB·s from the backend, shown as GB·h.
  function gbHours(mibSeconds) {
    if (typeof mibSeconds !== "number" || mibSeconds <= 0) return null;
    const gbh = mibSeconds / 1024 / 3600;
    if (gbh < 10) return `${gbh.toFixed(1)} GB·h`;
    if (gbh < 1000) return `${Math.round(gbh)} GB·h`;
    if (gbh < 1_000_000) return `${(gbh / 1000).toFixed(1)}K GB·h`;
    return `${(gbh / 1_000_000).toFixed(1)}M GB·h`;
  }

  let savedLine = $derived(
    gbHours(data.savings?.total_saved_mib_s ?? data.status?.total_saved_mib_s),
  );
</script>

<svelte:head>
  <title>Ember · scale to zero · jomcgi.dev</title>
  <meta
    name="description"
    content="Ember runs real services on Firecracker microVMs that freeze to disk when idle and restore in tens of milliseconds. A live Postgres you can wake, and an explainer of how the freeze works."
  />
</svelte:head>

<div class="ember-site">
  <header class="topbar">
    <span
      ><a class="brand" href="/"><strong>jomcgi.dev</strong></a> / ember</span
    >
  </header>

  <main class="ember-page">
    <header class="masthead">
      <h1><span class="ember-word">Ember</span></h1>
      <p class="lede">
        Services that scale to zero. When nobody is using one, the whole
        microVM is frozen to disk: 0 vCPU, 0 MiB of RAM. The first request
        restores it from the snapshot in tens of milliseconds, warm state and
        all.
      </p>
    </header>

    <div class="cards">
      <a class="card" href="/ember/postgres">
        <span class="kicker">live demo</span>
        <h2>Ember Postgres</h2>
        <p>
          A real Postgres that sleeps between visitors. Run a query and watch
          it wake: the fastest relight measured so far is 78&nbsp;ms.
        </p>
        {#if stateWord}
          <p class="live">
            <span class="dot" class:awake></span>
            <span
              >{stateWord} right now{savedLine
                ? ` · ${savedLine} of RAM-time saved`
                : ""}</span
            >
          </p>
        {/if}
        <span class="go" aria-hidden="true">wake it →</span>
      </a>

      <a class="card" href="/ember/firecracker">
        <span class="kicker">how it works</span>
        <h2>Boot once, restore forever</h2>
        <p>
          The mechanism behind the demo: a Firecracker microVM is booted and
          frozen once, then every request restores it in about 22&nbsp;ms.
        </p>
        <p class="live">a scroll-through of one real request</p>
        <span class="go" aria-hidden="true">see the freeze →</span>
      </a>
    </div>

    <p class="foot">
      Every number on these pages is a live measurement from this cluster, not
      a mockup.
    </p>
  </main>
</div>

<style>
  .ember-site {
    min-height: 100dvh;
    display: flex;
    flex-direction: column;
  }

  .topbar {
    display: flex;
    align-items: center;
    padding: 14px 28px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-muted);
  }

  .topbar strong {
    color: var(--em-ink);
    font-weight: 600;
  }

  .topbar .brand {
    color: inherit;
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar .brand:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .topbar .brand:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .ember-page {
    width: 100%;
    max-width: 880px;
    margin: 0 auto;
    padding: 6vh 24px 48px;
    display: flex;
    flex-direction: column;
    gap: 28px;
  }

  .masthead {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .masthead h1 {
    margin: 0;
    font-size: clamp(40px, 6vw, 56px);
    font-weight: 800;
    letter-spacing: -0.03em;
    line-height: 1;
  }

  .ember-word {
    background: linear-gradient(
      100deg,
      var(--em-ember-deep),
      var(--em-ember) 55%,
      var(--em-amber)
    );
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  .lede {
    margin: 0;
    max-width: 56ch;
    font-size: 16px;
    line-height: 1.6;
    color: var(--em-muted);
  }

  .cards {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }

  .card {
    position: relative;
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 22px 22px 20px;
    background: var(--em-panel);
    border: 1px solid var(--em-line);
    border-radius: 12px;
    box-shadow: var(--em-shadow-soft);
    text-decoration: none;
    color: var(--em-ink);
    transition:
      border-color 200ms ease,
      box-shadow 200ms ease,
      transform 200ms ease;
  }

  .card:hover {
    border-color: var(--em-ember-dim);
    box-shadow: var(--em-shadow);
    transform: translateY(-2px);
  }

  .card:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .kicker {
    font-family: var(--em-mono);
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--em-ember-deep);
  }

  .card h2 {
    margin: 0;
    font-size: 20px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }

  .card p {
    margin: 0;
    font-size: 14px;
    line-height: 1.55;
    color: var(--em-muted);
  }

  .live {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: auto;
    padding-top: 10px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-faint);
  }

  .dot {
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--em-frost);
    animation: breathe 3.2s ease-in-out infinite;
  }

  .dot.awake {
    background: var(--em-ember);
    animation-duration: 1.4s;
  }

  @keyframes breathe {
    0%,
    100% {
      opacity: 1;
      transform: scale(1);
    }
    50% {
      opacity: 0.45;
      transform: scale(0.8);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .dot {
      animation: none;
    }
    .card,
    .card:hover {
      transition: none;
      transform: none;
    }
  }

  .go {
    position: absolute;
    right: 20px;
    bottom: 18px;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-ember-deep);
    opacity: 0;
    transform: translateX(-4px);
    transition:
      opacity 200ms ease,
      transform 200ms ease;
  }

  .card:hover .go,
  .card:focus-visible .go {
    opacity: 1;
    transform: translateX(0);
  }

  @media (prefers-reduced-motion: reduce) {
    .go {
      transition: none;
      transform: none;
    }
  }

  .foot {
    margin: 0;
    font-family: var(--em-mono);
    font-size: 12.5px;
    color: var(--em-faint);
  }

  @media (max-width: 720px) {
    .topbar {
      padding: 12px 16px;
    }

    .ember-page {
      padding: 4vh 16px 40px;
      gap: 22px;
    }

    .cards {
      grid-template-columns: 1fr;
    }

    .go {
      opacity: 1;
      transform: none;
    }
  }
</style>
