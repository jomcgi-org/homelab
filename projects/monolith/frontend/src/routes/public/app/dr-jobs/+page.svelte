<script>
  import { onMount } from "svelte";
  import { goto, invalidateAll } from "$app/navigation";
  import { page } from "$app/stores";
  import { Button, Field } from "@homelab/design-system/components";
  import SiteIndex from "$lib/public/components/SiteIndex.svelte";
  import TechnicalDrawingChrome from "$lib/public/components/TechnicalDrawingChrome.svelte";
  import Trail from "$lib/public/components/Trail.svelte";
  import "$lib/public/styles/technical-drawing.css";
  import {
    readDrJobsParams,
    writeDrJobsParams,
  } from "$lib/public/dr-jobs/urlParams.js";

  let { data } = $props();

  const jobs = $derived(data.listings?.jobs ?? []);
  const liveJobs = $derived(jobs.filter((j) => j.is_live));
  const historyJobs = $derived(jobs.filter((j) => !j.is_live));

  // View + town filter are initialized from the URL (so a shared link restores
  // them) and mirrored back as they change (see the $effect below). Live is the
  // default; the History button reveals closed/expired posts (Option A
  // lifecycle keeps them, so this needs no second request).
  const initial = readDrJobsParams($page.url.searchParams);
  let view = $state(initial.view);
  let town = $state(initial.town);

  const bucket = $derived(view === "live" ? liveJobs : historyJobs);

  // Town options for the current bucket, alphabetised, with an "all" sentinel.
  const townOptions = $derived([
    { value: "", label: "All locations" },
    ...[...new Set(bucket.map((j) => j.town).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b))
      .map((t) => ({ value: t, label: t })),
  ]);

  const visible = $derived(
    town ? bucket.filter((j) => j.town === town) : bucket,
  );

  // Reset the town filter when the option leaves the new bucket (e.g. toggling
  // to History where that town has no posts), so the table never goes blank.
  // The reset mirrors to the URL via the effect below (town drops back to "").
  $effect(() => {
    if (town && !bucket.some((j) => j.town === town)) town = "";
  });

  // Mirror the view + town back to the URL so it is shareable. replaceState to
  // keep toggling and town picks out of browser history. Guarded: only goto
  // when the serialized params differ from the current URL, so this "URL write"
  // never re-triggers the init read in a loop.
  $effect(() => {
    const url = new URL($page.url);
    writeDrJobsParams(url.searchParams, { view, town });
    if (url.searchParams.toString() !== $page.url.searchParams.toString()) {
      goto(url, { keepFocus: true, noScroll: true, replaceState: true });
    }
  });

  const MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
  ];

  function fmtDate(iso) {
    if (!iso) return "n/a";
    const [y, m, d] = iso.split("-").map(Number);
    if (!y || !m || !d) return "n/a";
    return `${d} ${MONTHS[m - 1]} ${y}`;
  }

  // Whole days from today (UTC) to an ISO date; negative once past.
  function daysUntil(iso) {
    if (!iso) return null;
    const [y, m, d] = iso.split("-").map(Number);
    if (!y) return null;
    const then = Date.UTC(y, m - 1, d);
    const now = new Date();
    const today = Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate(),
    );
    return Math.round((then - today) / 86_400_000);
  }

  function closesLabel(iso) {
    const n = daysUntil(iso);
    if (n === null) return "";
    if (n < 0) return "closed";
    if (n === 0) return "closes today";
    if (n === 1) return "1 day left";
    return `${n} days left`;
  }

  const isClosingSoon = (iso) => {
    const n = daysUntil(iso);
    return n !== null && n >= 0 && n <= 7;
  };

  // "New" badge: first seen by the scraper within the last week.
  function isNew(j) {
    if (!j.first_seen_at) return false;
    const seen = Date.parse(j.first_seen_at);
    return Number.isFinite(seen) && Date.now() - seen < 7 * 86_400_000;
  }

  // Drop the leading "REF - " from the display title (the reference shows as its
  // own metadata value), falling back to the raw title if stripping leaves nothing.
  function cleanTitle(j) {
    if (j.reference && j.title.startsWith(j.reference)) {
      const rest = j.title
        .slice(j.reference.length)
        .replace(/^\s*[-–]\s*/, "")
        .trim();
      if (rest) return rest;
    }
    return j.title;
  }

  onMount(() => {
    // The scrape is hourly; this keeps an open tab from drifting if it sits for
    // a long time. Re-runs the SSR load (no client poll).
    const refresh = setInterval(() => invalidateAll(), 30 * 60_000);
    return () => clearInterval(refresh);
  });
</script>

<svelte:head>
  <title>NHS Scotland anaesthetics consultant jobs</title>
  <meta
    name="description"
    content="Aggregated NHS Scotland consultant and locum consultant anaesthetics vacancies, refreshed hourly from apply.jobs.scot.nhs.uk."
  />
</svelte:head>

{#snippet trail()}
  <Trail crumbs={[{ label: "dr-jobs" }]} label="Breadcrumb" />
{/snippet}

<TechnicalDrawingChrome {trail} />
<main class="td sheet dr-jobs">
  <div class="frame">
    <h1 class="sr-only">NHS Scotland anaesthetics consultant vacancies</h1>
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">{@render trail()}</div>
        <SiteIndex here="dr-jobs" />
      </aside>
      <section class="content edition" aria-label="Jobs">
        <header>
          <div class="ed-head">
            <p class="stats">
              <strong>{liveJobs.length}</strong> live
              <span class="stats-sep">&middot;</span>
              {historyJobs.length} past
            </p>
          </div>

          <div class="ed-lead">
            <p class="source">
              NHS Scotland consultant &amp; locum consultant anaesthetics,
              hourly from
              <a href="https://apply.jobs.scot.nhs.uk/Home/Job" rel="external"
                >apply.jobs.scot.nhs.uk</a
              >
            </p>
          </div>

          <div class="controls">
            <div
              class="toggle"
              role="group"
              aria-label="Show live or past jobs"
            >
              <Button
                variant="secondary"
                class="seg"
                aria-pressed={view === "live"}
                onclick={() => (view = "live")}
              >
                Live ({liveJobs.length})
              </Button>
              <Button
                variant="secondary"
                class="seg"
                aria-pressed={view === "history"}
                onclick={() => (view = "history")}
              >
                Past ({historyJobs.length})
              </Button>
            </div>

            <div class="location-filter">
              <Field label="Filter by location">
                {#snippet control(attributes)}
                  <select {...attributes} bind:value={town}>
                    {#each townOptions as option (option.value)}
                      <option value={option.value}>{option.label}</option>
                    {/each}
                  </select>
                {/snippet}
              </Field>
            </div>
          </div>
        </header>

        {#if visible.length === 0}
          <p class="empty">
            {#if view === "live"}
              No live anaesthetics consultant posts right now. Try History for
              recent listings.
            {:else}
              No past listings recorded yet.
            {/if}
          </p>
        {:else}
          <ul class="rows">
            {#each visible as j (j.job_id)}
              <li>
                <a
                  class="row"
                  class:past={!j.is_live}
                  href={j.url}
                  target="_blank"
                  rel="noopener"
                  aria-label={`${cleanTitle(j)}${j.town ? `, ${j.town}` : ""}, closes ${fmtDate(j.closing_date)}`}
                >
                  <span class="r-body">
                    <span class="r-head">
                      <span class="r-title">{cleanTitle(j)}</span>
                      {#if j.is_live && isNew(j)}
                        <span class="badge new">New</span>
                      {/if}
                    </span>
                    <span class="r-meta">
                      {#if j.reference}<span class="ref">{j.reference}</span
                        >{/if}
                      {#if j.town}<span>{j.town}</span>{/if}
                      {#if j.employment_type}<span>{j.employment_type}</span
                        >{/if}
                      {#if j.salary_band}<span>{j.salary_band}</span>{/if}
                    </span>
                  </span>
                  <span class="r-when">
                    <span class="r-date">{fmtDate(j.closing_date)}</span>
                    <span
                      class="r-rel"
                      class:urgent={isClosingSoon(j.closing_date)}
                    >
                      {closesLabel(j.closing_date)}
                    </span>
                  </span>
                </a>
              </li>
            {/each}
          </ul>
        {/if}
      </section>
    </div>
  </div>
</main>

<style>
  .stats {
    margin: 0;
  }
  .stats strong {
    color: var(--ink);
  }
  .source {
    overflow-wrap: anywhere;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 1rem;
    padding: 0 1rem 1rem;
  }
  .toggle {
    display: flex;
    flex: none;
  }
  /* Shared buttons keep native pressed/focus semantics. A horizontal strip
     marks the selection underneath, in the sheet's accent ink. */
  .toggle :global(.seg) {
    font-family: var(--font-code);
    font-size: 0.8rem;
    border-color: var(--stroke);
    border-bottom: 2px solid transparent;
    background: var(--sheet);
    color: var(--ink-2);
  }
  .toggle :global(.seg + .seg) {
    margin-left: -1px;
  }
  .toggle :global(.seg[aria-pressed="true"]) {
    color: var(--accent-ink);
    border-bottom-color: var(--accent-ink);
  }
  .location-filter {
    flex: 1 1 15rem;
    min-width: 0;
    font-size: 0.85rem;
  }
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .rows li {
    border-top: 1px solid var(--stroke);
  }
  .row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: center;
    gap: 1rem;
    padding: 0.85rem 1rem;
    text-decoration: none;
  }
  .row:hover,
  .row:focus-visible {
    background: var(--band);
  }
  /* Keep closed listings readable, including metadata, in both schemes. */
  .row.past {
    color: var(--ink-2);
  }
  .r-body {
    min-width: 0;
  }
  .r-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.5rem;
  }
  .r-title {
    font-size: 1rem;
    font-weight: 700;
    line-height: 1.4;
    overflow-wrap: anywhere;
    text-decoration: underline;
    text-decoration-color: var(--stroke);
    text-underline-offset: 0.2em;
  }
  .row:hover .r-title {
    text-decoration-color: var(--accent-ink);
  }
  /* Metadata is one divided value, including references. Long unbroken
     source values wrap inside the column instead of widening the sheet. */
  .r-meta {
    display: flex;
    flex-wrap: wrap;
    margin-top: 0.4rem;
    color: var(--ink-2);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }
  .r-meta > span {
    min-width: 0;
    padding-right: 0.6em;
  }
  .r-meta > span + span {
    border-left: 1px solid var(--stroke);
    padding-left: 0.6em;
  }
  .ref {
    font-family: var(--font-code);
  }
  .r-when {
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
    font-family: var(--font-code);
    font-size: 0.8rem;
    color: var(--ink-2);
  }
  .r-date {
    white-space: nowrap;
  }
  .r-rel.urgent {
    color: var(--warn);
    font-weight: 600;
  }
  .badge {
    border: 1px solid currentColor;
    padding: 0 0.4em;
    font-family: var(--font-code);
    font-size: 0.7rem;
    /* --ok alone measured 3.97:1 on the day sheet at this small size.
       Mixing with ink keeps the positive hue above AA in both schemes. */
    color: color-mix(in srgb, var(--ok) 80%, var(--ink));
  }
  .empty {
    margin: 0;
    padding: 1rem;
    border-top: 1px solid var(--stroke);
    color: var(--ink-2);
  }
  @media (max-width: 460px) {
    .row {
      grid-template-columns: minmax(0, 1fr);
      gap: 0.6rem;
    }
    .r-when {
      flex-direction: row;
      flex-wrap: wrap;
      gap: 0.8rem;
    }
  }
</style>
