<script>
  // /ember/postgres: the live scale-to-zero Postgres exhibit (embervm R4).
  // The console is a real database: every click here connects to the actual
  // demo VM through the same wake-on-connect path the private panel uses,
  // Turnstile-gated and rate-limited for public traffic (see the design doc,
  // docs/plans/2026-07-18-ember-public-pages-design.md).
  //
  // Visual language: the /ember/* pages are their own small site in the
  // fcstory palette (lib/public/ember/ember.css), not the neobrutalist
  // jomcgi.dev baseline. The topbar wordmark is the only nav: it links home.
  import EmberConsole from "$lib/public/ember/EmberConsole.svelte";
  import EmberStage from "$lib/public/ember/EmberStage.svelte";
  import "$lib/public/ember/ember.css";

  let { data } = $props();

  // EmberConsole owns the one poll loop for this page (status/running/
  // stopwatchMs); these are bindable props on the console so the stage can
  // read the same live values without a second poller.
  let consoleStatus = $state(null);
  let consoleRunning = $state(false);
  let consoleStopwatchMs = $state(0);
</script>

<svelte:head>
  <title>Scale-to-zero Postgres · jomcgi.dev</title>
  <meta
    name="description"
    content="A Postgres microVM that banks itself to disk about a second after its last connection closes and wakes on the next connect. Query it live: every number on this page is a real measurement, not a mockup."
  />
</svelte:head>

<div class="ember-site">
  <header class="topbar">
    <span
      ><a class="brand" href="/"><strong>jomcgi.dev</strong></a> / ember /
      postgres</span
    >
    <a class="topbar-cross" href="/ember/firecracker">how does firecracker work?</a>
  </header>

  <main class="ember-page">
    <!-- The fold: claim on the left, live proof on the right, console
         controls directly below. A visitor on a ~900px window sees the state,
         the headline number, and both buttons without scrolling; the prose
         explainers live below the fold. -->
    <div class="fold">
      <header class="ember-hero">
        <h1>
          A database that <span class="frost-word">sleeps</span> when nobody's
          asking.
        </h1>
        <p class="lede">
          Banks itself to disk a second after the last connection closes, then
          costs nothing. The next query
          <span class="ember-word">wakes it</span> in under a second, on the
          exact data it left behind.
        </p>
      </header>

      <EmberStage
        vmState={consoleStatus?.state}
        totalSavedMibS={consoleStatus?.total_saved_mib_s}
        stopwatchMs={consoleStopwatchMs}
        running={consoleRunning}
      />
    </div>

    <EmberConsole
      turnstileSiteKey={data.turnstileSiteKey}
      initialStatus={data.initialStatus}
      initialSavings={data.initialSavings}
      bind:status={consoleStatus}
      bind:running={consoleRunning}
      bind:stopwatchMs={consoleStopwatchMs}
    />

    <section class="explainer">
      <h2>Why the data survives</h2>
      <p>
        The orders table lives on a separate volume from the snapshot, so
        destroying the VM never touches rows already written. A cold boot
        against that volume is slower than a snapshot resume but recovers
        every row: the compute is disposable, the data is not. "Wake +
        connect" on the console is wall-clock time from the first TCP packet
        to a usable connection, whichever path the wake took.
      </p>
    </section>

    <footer class="ember-foot">
      <p>
        The freeze and restore underneath this demo:
        <a href="/ember/firecracker">boot once, restore forever</a>.
      </p>
    </footer>
  </main>
</div>

<style>
  .ember-site {
    min-height: 100dvh;
  }

  .topbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
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

  .topbar .brand:focus-visible,
  .topbar-cross:focus-visible {
    outline: 2px solid var(--em-ember-deep);
    outline-offset: 3px;
  }

  .topbar-cross {
    color: var(--em-muted);
    text-decoration: none;
    border-radius: 4px;
  }

  .topbar-cross:hover {
    color: var(--em-ember-deep);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .ember-page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 12px 24px 80px;
    display: flex;
    flex-direction: column;
    gap: 24px;
  }

  .fold {
    display: grid;
    grid-template-columns: 340px 1fr;
    gap: 16px;
    align-items: center;
  }

  .ember-hero {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .ember-hero h1 {
    margin: 0;
    font-size: clamp(26px, 2.8vw, 36px);
    font-weight: 800;
    letter-spacing: -0.025em;
    line-height: 1.08;
    color: var(--em-ink);
    text-wrap: balance;
  }

  .frost-word {
    color: var(--em-frost);
  }

  .ember-word {
    color: var(--em-ember);
    font-weight: 600;
  }

  .lede {
    margin: 0;
    font-size: 15px;
    line-height: 1.55;
    color: var(--em-muted);
    max-width: 44ch;
  }

  .explainer {
    max-width: 720px;
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  .explainer h2 {
    margin: 0;
    font-size: clamp(19px, 1.8vw, 23px);
    font-weight: 750;
    letter-spacing: -0.02em;
    color: var(--em-ink);
    text-wrap: balance;
  }

  .explainer p {
    margin: 0;
    font-size: 15px;
    line-height: 1.6;
    color: var(--em-muted);
  }

  .ember-foot {
    border-top: 1px solid var(--em-line);
    padding-top: 20px;
    max-width: 720px;
  }

  .ember-foot p {
    margin: 0;
    font-size: 14px;
    color: var(--em-faint);
  }

  .ember-foot a {
    color: var(--em-ember-deep);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  @media (max-width: 900px) {
    .topbar {
      padding: 12px 16px;
    }

    .ember-page {
      padding: 8px 16px 64px;
      gap: 18px;
    }

    .fold {
      grid-template-columns: 1fr;
      gap: 14px;
    }

    .ember-hero h1 {
      font-size: clamp(24px, 6.4vw, 30px);
    }

    .lede {
      font-size: 14px;
      max-width: none;
    }
  }
</style>
