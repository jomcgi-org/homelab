<script>
  import { goto } from "$app/navigation";
  import { page } from "$app/stores";
  import TripMap from "$lib/public/components/trips/TripMap.svelte";
  import PhotoGrid from "$lib/public/components/trips/PhotoGrid.svelte";
  import TagFilter from "$lib/public/components/trips/TagFilter.svelte";
  import ViewToggle from "$lib/public/components/trips/ViewToggle.svelte";
  import { groupByDay, dayPhotos } from "$lib/trips/trip.js";

  // Simpler than the old React timeline (which had playback, virtualization and
  // a live socket): a filterable chronological gallery plus the route map.
  let { data } = $props();

  const trip = $derived(data.trip);
  const points = $derived(data.points ?? []);
  const days = $derived(groupByDay(points, trip?.tz));
  const photos = $derived(points.filter((p) => p.image));

  const availableTags = $derived(
    [...new Set(photos.flatMap((p) => p.tags ?? []))].sort((a, b) =>
      a.localeCompare(b),
    ),
  );

  let selected = $state([]);
  const filtered = $derived(
    selected.length === 0
      ? photos
      : photos.filter((p) =>
          (p.tags ?? []).some((t) => selected.includes(t.toLowerCase())),
        ),
  );

  // The photos/map toggle is mirrored to the URL (?view=) so a shared link opens
  // on the same view. The [slug] already lives in the path; this is the only
  // view-state param. Validate against the allow-list and fall back to "photos".
  const VIEWS = new Set(["photos", "map"]);
  const rawView = $page.url.searchParams.get("view");
  let view = $state(VIEWS.has(rawView) ? rawView : "photos");

  // Mirror the view back to the URL. replaceState so flipping photos/map does
  // not stack browser-history entries. "photos" is the default, so it is dropped
  // to keep the shared URL clean. Guarded: only goto when the serialized params
  // differ, so this "URL write" never re-triggers the init read in a loop.
  $effect(() => {
    const url = new URL($page.url);
    if (view === "map") url.searchParams.set("view", "map");
    else url.searchParams.delete("view");
    if (url.searchParams.toString() !== $page.url.searchParams.toString()) {
      goto(url, { keepFocus: true, noScroll: true, replaceState: true });
    }
  });
</script>

<svelte:head>
  <title
    >{trip ? `${trip.short_title ?? trip.title} - Timeline` : "Timeline"}</title
  >
</svelte:head>

<div class="page">
  <header class="head">
    <nav class="crumb" aria-label="Breadcrumb">
      <a class="crumb-link" href="/app/trips">trips</a>
      <span class="crumb-sep">/</span>
      <a class="crumb-link" href={`/app/trips/${trip?.slug}`}
        >{trip?.short_title ?? trip?.slug}</a
      >
      <span class="crumb-sep">/</span>
      <span class="crumb-name">timeline</span>
    </nav>

    <div class="controls">
      <ViewToggle
        bind:value={view}
        options={[
          { value: "photos", label: "Photos" },
          { value: "map", label: "Map" },
        ]}
      />
      <p class="count">{filtered.length} / {photos.length} photos</p>
    </div>

    {#if availableTags.length}
      <div class="tags">
        <TagFilter tags={availableTags} bind:selected />
      </div>
    {/if}
  </header>

  {#if view === "map"}
    <div class="map-box">
      <TripMap
        {days}
        onDayClick={(n) =>
          (window.location.href = `/app/trips/${trip.slug}/day/${n}`)}
      />
    </div>
  {:else}
    <PhotoGrid photos={filtered} tz={trip?.tz} />
  {/if}
</div>

<style>
  .page {
    max-width: 1280px;
    margin: 0 auto;
    padding: 24px 24px 64px;
    background: var(--cream);
    color: var(--ink);
    min-height: 100vh;
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
    margin-bottom: 18px;
  }
  .crumb-link {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
    padding: 0 2px;
  }
  .crumb-link:hover {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
  }
  .crumb-sep {
    color: var(--ink-3);
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .count {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin: 0;
  }
  .tags {
    margin-bottom: 20px;
  }
  .map-box {
    border: 2px solid var(--ink);
    height: 70vh;
  }
</style>
