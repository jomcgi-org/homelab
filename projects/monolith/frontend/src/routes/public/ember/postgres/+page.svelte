<script>
  // /ember/postgres: the live scale-to-zero Postgres exhibit (embervm R4).
  // The console is a real database: every click here connects to the actual
  // demo VM through the same wake-on-connect path the private panel uses,
  // Turnstile-gated and rate-limited for public traffic (see the design doc,
  // the ember public-pages design).
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
  let consoleWakePromise = $state("");
</script>

<svelte:head>
  <title>Ember Postgres</title>
  <meta
    name="description"
    content="Postgres scaling to zero on Firecracker microVMs with sub-second resume from disk to memory. Query it live. Every number is a real measurement, baked in at build time."
  />
</svelte:head>

<div class="ember-site">
  <header class="topbar">
    <span
      ><a class="brand" href="/"><strong>jomcgi.dev</strong></a> /
      <a class="brand" href="/ember">ember</a> / postgres</span
    >
    <a class="topbar-cross" href="/ember/firecracker"
      >how firecracker resumes a VM</a
    >
  </header>

  <main class="ember-page">
    <header class="masthead">
      <h1><span class="ember-word">Ember</span> Postgres</h1>
      <p class="subtitle">
        A real Postgres that sleeps when nobody is using it. Wake it yourself
        and watch the clock.
      </p>
    </header>

    <EmberStage
      vmState={consoleStatus?.state}
      totalSavedMibS={consoleStatus?.total_saved_mib_s}
      stopwatchMs={consoleStopwatchMs}
      running={consoleRunning}
      wakePromise={consoleWakePromise}
      present={consoleStatus?.present}
    />

    <EmberConsole
      turnstileSiteKey={data.turnstileSiteKey}
      initialStatus={data.initialStatus}
      initialSavings={data.initialSavings}
      bind:status={consoleStatus}
      bind:running={consoleRunning}
      bind:stopwatchMs={consoleStopwatchMs}
      bind:wakePromise={consoleWakePromise}
    />
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
    padding: 4px 24px 48px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .masthead {
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding-bottom: 4px;
  }

  .masthead h1 {
    margin: 0;
    font-size: clamp(24px, 2.4vw, 30px);
    font-weight: 800;
    letter-spacing: -0.02em;
    line-height: 1.1;
    color: var(--em-ink);
  }

  .ember-word {
    color: var(--em-ember);
  }

  .subtitle {
    margin: 0;
    font-size: 14.5px;
    line-height: 1.5;
    color: var(--em-muted);
  }

  @media (max-width: 900px) {
    .topbar {
      padding: 12px 16px;
    }

    .ember-page {
      padding: 4px 16px 48px;
      gap: 14px;
    }
  }
</style>
