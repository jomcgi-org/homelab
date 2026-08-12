<script>
  import { onMount } from "svelte";
  import { goto, invalidateAll } from "$app/navigation";
  import { page } from "$app/stores";
  import BrutalistSelect from "$lib/public/components/BrutalistSelect.svelte";
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
  // own chip), falling back to the raw title if stripping leaves nothing.
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

<div class="page">
  <h1 class="sr-only">NHS Scotland anaesthetics consultant vacancies</h1>

  <!-- One bordered data-sheet: header band on top, hairline-divided job rows
       below. No floating boxes, no resting shadows. -->
  <div class="board">
    <header class="board-head">
      <div class="crumb-row">
        <nav class="crumb" aria-label="Breadcrumb">
          <a class="crumb-home" href="/"
            >jomcgi.dev<span class="crumb-arrow" aria-hidden="true"
              >&nearr;</span
            ></a
          >
          <span class="crumb-sep">/</span>
          <span class="crumb-name">dr-jobs</span>
        </nav>
        <p class="stats">
          <strong>{liveJobs.length}</strong> live
          <span class="stats-sep">&middot;</span>
          {historyJobs.length} past
        </p>
      </div>

      <p class="source">
        NHS Scotland consultant &amp; locum consultant anaesthetics, hourly from
        <a href="https://apply.jobs.scot.nhs.uk/Home/Job" rel="external"
          >apply.jobs.scot.nhs.uk</a
        >
      </p>

      <div class="controls">
        <div class="toggle" role="group" aria-label="Show live or past jobs">
          <button
            class="seg"
            class:active={view === "live"}
            aria-pressed={view === "live"}
            onclick={() => (view = "live")}
          >
            Live ({liveJobs.length})
          </button>
          <button
            class="seg"
            class:active={view === "history"}
            aria-pressed={view === "history"}
            onclick={() => (view = "history")}
          >
            Past ({historyJobs.length})
          </button>
        </div>

        <BrutalistSelect
          options={townOptions}
          bind:value={town}
          label="Filter by location"
          id="dr-jobs-town"
        />
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
                  {#if j.reference}
                    <span class="badge ref">{j.reference}</span>
                  {/if}
                </span>
                <span class="r-meta">
                  {#if j.town}<span>{j.town}</span>{/if}
                  {#if j.employment_type}<span class="dot">&middot;</span><span
                      >{j.employment_type}</span
                    >{/if}
                  {#if j.salary_band}<span class="dot">&middot;</span><span
                      >{j.salary_band}</span
                    >{/if}
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
  </div>
</div>

<style>
  .page {
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--cream);
    color: var(--ink);
    padding: 16px 12px;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }

  /* The whole app is one hard-edged sheet: 2px ink border, sharp corners, no
     shadow. Narrow column so the dense rows do not sprawl. */
  .board {
    max-width: 680px;
    margin: 0 auto;
    background: var(--paper);
    border: 2px solid var(--ink);
  }

  /* Masthead: same white paper as the rows (no cream-on-cream tint), set off
     from the row body only by the 2px rule. */
  .board-head {
    display: flex;
    flex-direction: column;
    gap: 9px;
    padding: 12px 14px;
    border-bottom: 2px solid var(--ink);
  }

  .crumb-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px 14px;
    flex-wrap: wrap;
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
  }

  .crumb-home {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
    padding: 0 2px;
    transition: background 140ms ease;
  }

  .crumb-home:hover,
  .crumb-home:focus-visible {
    background: linear-gradient(transparent 56%, var(--accent) 56%);
    text-decoration-color: var(--ink);
  }

  .crumb-arrow {
    font-size: 0.85em;
  }

  .stats {
    margin: 0;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    white-space: nowrap;
  }

  .stats strong {
    color: var(--ink);
    font-weight: 700;
  }

  .stats-sep {
    opacity: 0.5;
  }

  .source {
    margin: 0;
    font-size: 12px;
    line-height: 1.4;
    color: var(--ink-3);
  }

  .source a {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 10px;
    margin-top: 1px;
  }

  /* LIVE / HISTORY: two self-bordered boxes, flat at rest, that lift off
     leaving a hard shadow on hover (they ARE clickable, unlike the rows). */
  .toggle {
    display: inline-flex;
  }

  .seg {
    position: relative;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 7px 13px;
    background: var(--paper);
    color: var(--ink);
    border: 2px solid var(--ink);
    cursor: pointer;
    transition:
      transform 120ms ease,
      box-shadow 120ms ease,
      background 120ms ease;
  }

  .seg + .seg {
    margin-left: -2px;
  }

  .seg.active {
    background: var(--accent);
    z-index: 1;
  }

  .seg:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
    z-index: 2;
  }

  /* Job rows: a hairline-divided ledger. Each row is a whole-row link, so the
     feedback is a flat accent wash on hover (NOT a lift, which would imply a
     box that pops but goes nowhere). */
  .rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }

  .rows li + li {
    border-top: 2px solid var(--ink);
  }

  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    padding: 9px 14px;
    text-decoration: none;
    color: var(--ink);
    transition: background 100ms ease;
  }

  .row:hover,
  .row:focus-visible {
    background: var(--accent);
    outline: none;
  }

  .row.past {
    opacity: 0.55;
  }

  .r-body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .r-head {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 7px;
  }

  .r-title {
    font-size: 15px;
    font-weight: 700;
    line-height: 1.25;
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
  }

  .row:hover .r-title,
  .row:focus-visible .r-title {
    text-decoration-color: var(--ink);
  }

  .r-meta {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 5px;
    font-size: 12px;
    color: var(--ink-3);
  }

  .row:hover .r-meta,
  .row:focus-visible .r-meta {
    color: var(--ink);
  }

  .r-meta .dot {
    opacity: 0.5;
  }

  .r-when {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 1px;
    text-align: right;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .r-date {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
  }

  .r-rel {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.02em;
    color: var(--ink-3);
  }

  .row:hover .r-rel,
  .row:focus-visible .r-rel {
    color: var(--ink);
  }

  .r-rel.urgent {
    color: var(--coral);
    font-weight: 700;
  }

  .row:hover .r-rel.urgent,
  .row:focus-visible .r-rel.urgent {
    color: var(--ink);
  }

  /* Status pills: green = fresh, outline = board reference. */
  .badge {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 1px 7px;
    border: 1.5px solid var(--ink);
    border-radius: 999px;
    white-space: nowrap;
  }

  .badge.new {
    background: var(--green);
  }

  .badge.ref {
    background: transparent;
    color: var(--ink);
  }

  .empty {
    margin: 0;
    padding: 16px 14px;
    font-size: 13px;
    line-height: 1.5;
    color: var(--ink-3);
  }

  @media (max-width: 460px) {
    .row {
      flex-direction: column;
      align-items: stretch;
      gap: 6px;
    }
    .r-when {
      flex-direction: row;
      align-items: baseline;
      gap: 8px;
      text-align: left;
    }
  }

  /* Desktop scale-up: the 680px mobile-first column gets lost in the middle of a
     wide viewport ("tiny on a normal page"). Past the tablet breakpoint, widen
     the sheet and step the dense-ledger type up a notch so it reads at a
     comfortable desktop size. Mobile (< 768px) keeps the compact sizing that
     already works. The location <select> sets its own size inside
     BrutalistSelect (Svelte scoping puts it out of reach here). */
  @media (min-width: 768px) {
    .page {
      padding: 32px 24px;
    }
    .board {
      max-width: 940px;
    }
    .board-head {
      gap: 11px;
      padding: 16px 22px;
    }
    .crumb,
    .stats,
    .source {
      font-size: 13px;
    }
    .seg {
      font-size: 13px;
      padding: 8px 16px;
    }
    .row {
      padding: 13px 22px;
    }
    .r-title {
      font-size: 17px;
    }
    .r-meta {
      font-size: 13px;
    }
    .r-date {
      font-size: 15px;
    }
    .r-rel {
      font-size: 12px;
    }
    .empty {
      font-size: 15px;
      padding: 22px;
    }
  }
</style>
