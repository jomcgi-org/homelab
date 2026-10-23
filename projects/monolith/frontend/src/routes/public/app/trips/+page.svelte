<script>
  import TripSheet from "$lib/public/components/trips/TripSheet.svelte";
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

<TripSheet>
  <header class="head edition ed-lead">
    <h1>Trips</h1>
    <p class="lede">Journeys mapped from GPS-tagged photos.</p>
  </header>

  {#if trips.length}
    <ul class="grid">
      {#each trips as trip (trip.slug)}
        <li>
          <a class="card edition" href={`/app/trips/${trip.slug}`}>
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
</TripSheet>

<style>
  .head {
    margin-bottom: 1.5rem;
  }
  .grid {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 17rem), 1fr));
    gap: 1.5rem;
  }
  .grid li {
    min-width: 0;
  }
  .card {
    display: block;
    text-decoration: none;
    color: var(--ink);
  }
  .card:hover h2 {
    color: var(--accent-ink);
  }
  .thumb {
    aspect-ratio: 3 / 2;
    overflow: hidden;
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
  }
  .thumb img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    display: block;
  }
  .meta {
    padding: 1rem;
  }
  .meta h2 {
    font-size: 1.25rem;
    line-height: 1.2;
    margin: 0;
  }
  .sub,
  .empty {
    font-family: var(--font-code);
    font-size: 0.8rem;
    color: var(--ink-2);
  }
</style>
