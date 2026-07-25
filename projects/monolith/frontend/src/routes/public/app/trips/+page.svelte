<script>
  // Image URLs are pre-signed server-side in +page.server.js (trip.coverUrl);
  // the signing secret never reaches the client.
  let { data } = $props();

  const trips = $derived(data.index?.trips ?? []);
</script>

<svelte:head>
  <title>Trips, GPS photo journeys</title>
  <meta
    name="description"
    content="Road trips and journeys mapped from GPS-tagged photos: routes, day-by-day galleries and elevation."
  />
</svelte:head>

<div class="page">
  <header class="head">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-home" href="https://jomcgi.dev/"
        >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span
        ></a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">trips</span>
    </nav>
    <h1>Trips</h1>
    <p class="lede">Journeys mapped from GPS-tagged photos.</p>
  </header>

  {#if trips.length}
    <ul class="grid">
      {#each trips as trip (trip.slug)}
        <li>
          <a class="card" href={`/app/trips/${trip.slug}`}>
            <div class="thumb">
              {#if trip.coverUrl}
                <img
                  src={trip.coverUrl}
                  alt={trip.title}
                  loading="lazy"
                  decoding="async"
                />
              {:else}
                <div class="thumb-empty" aria-hidden="true"></div>
              {/if}
            </div>
            <div class="meta">
              <h2>{trip.title}</h2>
              {#if trip.subtitle}<p class="sub">{trip.subtitle}</p>{/if}
            </div>
          </a>
        </li>
      {/each}
    </ul>
  {:else}
    <p class="empty">No trips yet.</p>
  {/if}
</div>

<style>
  .page {
    max-width: 1100px;
    margin: 0 auto;
    padding: 32px 24px 64px;
    background: var(--cream);
    color: var(--ink);
    min-height: 100vh;
  }
  .head {
    margin-bottom: 32px;
  }
  .crumb {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 16px;
  }
  .crumb-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
    padding: 0 2px;
  }
  .crumb-home:hover {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
  }
  .crumb-sep {
    color: var(--ink-3);
  }
  h1 {
    font-family: var(--serif);
    font-size: 48px;
    line-height: 1;
    margin: 0 0 8px;
  }
  .lede {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink-2);
    margin: 0;
  }
  .grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 20px;
  }
  .card {
    display: block;
    border: 2px solid var(--ink);
    background: var(--paper);
    text-decoration: none;
    color: var(--ink);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }
  .card:hover {
    transform: translate(-3px, -3px);
    box-shadow: 4px 4px 0 var(--ink);
  }
  .thumb {
    aspect-ratio: 3 / 2;
    overflow: hidden;
    border-bottom: 2px solid var(--ink);
    background: var(--ink);
  }
  .thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .thumb-empty {
    width: 100%;
    height: 100%;
    background: repeating-linear-gradient(
      45deg,
      var(--cream),
      var(--cream) 8px,
      var(--paper) 8px,
      var(--paper) 16px
    );
  }
  .meta {
    padding: 14px 16px;
  }
  .meta h2 {
    font-family: var(--serif);
    font-size: 22px;
    line-height: 1.1;
    margin: 0;
  }
  .sub {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    margin: 6px 0 0;
  }
  .empty {
    font-family: var(--mono);
    color: var(--ink-3);
  }
</style>
