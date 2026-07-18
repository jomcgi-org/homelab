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
    <a class="topbar-cross" href="/ember/firecracker">how the freeze works</a>
  </header>

  <main class="ember-page">
    <header class="ember-hero">
      <h1>
        A database that <span class="frost-word">sleeps</span> when nobody's
        asking.
      </h1>
      <p class="lede">
        This Postgres microVM banks itself to disk about a second after its
        last connection closes, so it costs nothing while idle. The next query
        <span class="ember-word">wakes it</span>, usually in well under a
        second, against the exact data it left behind.
      </p>
    </header>

    <EmberStage
      vmState={consoleStatus?.state}
      totalSavedMibS={consoleStatus?.total_saved_mib_s}
      stopwatchMs={consoleStopwatchMs}
      running={consoleRunning}
    />

    <EmberConsole
      turnstileSiteKey={data.turnstileSiteKey}
      initialStatus={data.initialStatus}
      initialSavings={data.initialSavings}
      bind:status={consoleStatus}
      bind:running={consoleRunning}
      bind:stopwatchMs={consoleStopwatchMs}
    />

    <section class="explainer">
      <h2>What "banking" means</h2>
      <p>
        Banking is a pause-to-disk, not a shutdown: the VM's live memory and CPU
        state are snapshotted and the process is torn down, leaving nothing
        running and nothing billed while it waits. A snapshot is a much faster
        thing to resume than a fresh boot is to perform, which is why most wakes
        land under a second instead of paying a full cold start.
      </p>
    </section>

    <section class="explainer">
      <h2>Why the data survives, and what the wake number means</h2>
      <p>
        The orders table lives on a separate data volume from the snapshot, so
        destroying the VM (or losing the snapshot entirely) never touches the
        rows already written. A fresh cold boot against that same volume is
        slower than resuming from a snapshot, but it recovers every row, which
        is the actual point of the exhibit: the compute is disposable, the data
        is not. The "wake + connect" number on the console is the wall-clock
        time from the first TCP packet to a usable connection, whichever path
        the VM had to take to get there.
      </p>
    </section>

    <footer class="ember-foot">
      <p>
        The freeze-and-restore trick underneath this demo has its own story:
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
    padding: 16px 28px;
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
    padding: 40px 24px 96px;
    display: flex;
    flex-direction: column;
    gap: 40px;
  }

  .ember-hero {
    display: flex;
    flex-direction: column;
    gap: 14px;
    max-width: 780px;
  }

  .ember-hero h1 {
    margin: 0;
    font-size: clamp(34px, 4.8vw, 56px);
    font-weight: 800;
    letter-spacing: -0.03em;
    line-height: 1.05;
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
    font-size: clamp(16px, 1.4vw, 19px);
    line-height: 1.6;
    color: var(--em-muted);
    max-width: 58ch;
  }

  .explainer {
    max-width: 720px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .explainer h2 {
    margin: 0;
    font-size: clamp(21px, 2vw, 26px);
    font-weight: 750;
    letter-spacing: -0.02em;
    color: var(--em-ink);
    text-wrap: balance;
  }

  .explainer p {
    margin: 0;
    font-size: 15.5px;
    line-height: 1.65;
    color: var(--em-muted);
  }

  .ember-foot {
    border-top: 1px solid var(--em-line);
    padding-top: 24px;
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
</style>
