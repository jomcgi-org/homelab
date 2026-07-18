<script>
  // /ember/postgres: the live scale-to-zero Postgres exhibit (embervm R4).
  // The console is a real database: every click here connects to the actual
  // demo VM through the same wake-on-connect path the private panel uses,
  // Turnstile-gated and rate-limited for public traffic (see the design doc,
  // docs/plans/2026-07-18-ember-public-pages-design.md).
  import EmberConsole from "$lib/public/ember/EmberConsole.svelte";

  let { data } = $props();
</script>

<svelte:head>
  <title>Scale-to-zero Postgres · jomcgi.dev</title>
  <meta
    name="description"
    content="A Postgres microVM that banks itself to disk about a second after its last connection closes and wakes on the next connect. Query it live: every number on this page is a real measurement, not a mockup."
  />
</svelte:head>

<main class="ember-page">
  <header class="ember-hero">
    <h1>A database that sleeps when nobody's asking.</h1>
    <p class="lede">
      This Postgres microVM banks itself to disk about a second after its
      last connection closes, so it costs nothing while idle. The next query
      wakes it, usually in well under a second, against the exact data it
      left behind.
    </p>
  </header>

  <!-- Task 8 (live ember stage) mounts here: a hot/cold RAM cell grid driven
       by the console's live status poll, above the console proper. -->

  <EmberConsole
    turnstileSiteKey={data.turnstileSiteKey}
    initialStatus={data.initialStatus}
    initialSavings={data.initialSavings}
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
</main>

<style>
  .ember-page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 48px 24px 96px;
    display: flex;
    flex-direction: column;
    gap: 40px;
  }

  .ember-hero {
    display: flex;
    flex-direction: column;
    gap: 12px;
    max-width: 760px;
  }

  .ember-hero h1 {
    font-family: var(--serif);
    font-size: clamp(32px, 4.5vw, 48px);
    font-weight: 400;
    line-height: 1.1;
    color: var(--ink);
  }

  .lede {
    font-size: 17px;
    line-height: 1.55;
    color: var(--ink-2);
  }

  .explainer {
    max-width: 720px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .explainer h2 {
    font-family: var(--serif);
    font-size: 24px;
    font-weight: 400;
    color: var(--ink);
  }

  .explainer p {
    font-size: 15px;
    line-height: 1.6;
    color: var(--ink-2);
  }
</style>
