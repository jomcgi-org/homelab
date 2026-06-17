<script>
  import { onMount } from "svelte";
  import { invalidateAll } from "$app/navigation";
  import BrutalistSelect from "$lib/public/components/BrutalistSelect.svelte";

  let { data } = $props();

  const jobs = $derived(data.listings?.jobs ?? []);
  const liveJobs = $derived(jobs.filter((j) => j.is_live));
  const historyJobs = $derived(jobs.filter((j) => !j.is_live));

  // Live by default; the History button reveals closed/expired posts (Option A
  // lifecycle keeps them, so this needs no second request).
  let view = $state("live");
  let town = $state("");

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
  $effect(() => {
    if (town && !bucket.some((j) => j.town === town)) town = "";
  });

  const MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
  ];

  function fmtDate(iso) {
    if (!iso) return "—";
    const [y, m, d] = iso.split("-").map(Number);
    if (!y || !m || !d) return "—";
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
    if (n === 0) return "today";
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
    // The scrape is daily, so a slow refresh is plenty; this keeps an open tab
    // from drifting if it sits for hours. Re-runs the SSR load (no client poll).
    const refresh = setInterval(() => invalidateAll(), 30 * 60_000);
    return () => clearInterval(refresh);
  });
</script>

<svelte:head>
  <title>NHS Scotland anaesthetics consultant jobs</title>
  <meta
    name="description"
    content="Aggregated NHS Scotland consultant and locum consultant anaesthetics vacancies, refreshed daily from apply.jobs.scot.nhs.uk."
  />
</svelte:head>

<div class="jobs-page">
  <h1 class="sr-only">NHS Scotland anaesthetics consultant vacancies</h1>

  <header class="head panel">
    <div class="crumb-row">
      <nav class="crumb" aria-label="Breadcrumb">
        <a class="crumb-home" href="https://jomcgi.dev/"
          >jomcgi.dev<span class="crumb-arrow" aria-hidden="true">&nearr;</span></a
        >
        <span class="crumb-sep">/</span>
        <span class="crumb-name">dr-jobs</span>
      </nav>
      <p class="stats">
        {liveJobs.length} live &middot; {historyJobs.length} past
      </p>
    </div>

    <p class="lede">
      NHS Scotland consultant &amp; locum consultant anaesthetics posts, scraped
      daily from
      <a href="https://apply.jobs.scot.nhs.uk/Home/Job" rel="external"
        >apply.jobs.scot.nhs.uk</a
      >.
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
          History ({historyJobs.length})
        </button>
      </div>

      <div class="field">
        <span class="field-label" id="town-label">Location</span>
        <BrutalistSelect
          options={townOptions}
          bind:value={town}
          label="Filter by location"
          id="dr-jobs-town"
        />
      </div>
    </div>
  </header>

  {#if visible.length === 0}
    <p class="empty panel">
      {#if view === "live"}
        No live anaesthetics consultant posts right now. Try History for recent
        listings.
      {:else}
        No past listings recorded yet.
      {/if}
    </p>
  {:else}
    <ul class="cards">
      {#each visible as j (j.job_id)}
        <li class="card panel" class:past={!j.is_live}>
          <div class="card-main">
            <div class="badges">
              {#if j.is_live && isNew(j)}
                <span class="badge new">New</span>
              {/if}
              {#if j.is_live && isClosingSoon(j.closing_date)}
                <span class="badge soon">Closing soon</span>
              {/if}
              {#if j.reference}
                <span class="badge ref">{j.reference}</span>
              {/if}
            </div>
            <a class="title" href={j.url} target="_blank" rel="noopener">
              {cleanTitle(j)}
            </a>
            <p class="meta">
              {#if j.town}<span>{j.town}{j.postcode ? ` ${j.postcode}` : ""}</span
                >{/if}
              {#if j.employment_type}<span class="dot">&middot;</span><span
                  >{j.employment_type}</span
                >{/if}
              {#if j.salary_band}<span class="dot">&middot;</span><span
                  >{j.salary_band}</span
                >{/if}
            </p>
          </div>
          <div class="card-side">
            <span class="close-date">{fmtDate(j.closing_date)}</span>
            <span class="close-rel" class:urgent={isClosingSoon(j.closing_date)}>
              {closesLabel(j.closing_date)}
            </span>
          </div>
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .jobs-page {
    min-height: 100vh;
    min-height: 100dvh;
    background: var(--cream);
    color: var(--ink);
    padding: 16px;
    max-width: 860px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 14px;
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

  /* Flat, sharp-bordered paper panels, matching the ships/hikes overlays. */
  .panel {
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 14px;
  }

  .head {
    display: flex;
    flex-direction: column;
    gap: 12px;
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

  .crumb-sep,
  .crumb-name {
    color: var(--ink);
  }

  .stats {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    margin: 0;
  }

  .lede {
    margin: 0;
    font-size: 14px;
    line-height: 1.5;
  }

  .lede a {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
  }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-end;
    gap: 12px;
  }

  .toggle {
    display: inline-flex;
    border: 2px solid var(--ink);
  }

  .seg {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 8px 14px;
    background: var(--paper);
    color: var(--ink);
    border: 0;
    cursor: pointer;
    transition: background 120ms ease;
  }

  .seg + .seg {
    border-left: 2px solid var(--ink);
  }

  .seg.active {
    background: var(--accent);
  }

  .seg:hover:not(.active) {
    background: var(--cream);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 180px;
  }

  .field-label {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .cards {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .card {
    display: flex;
    align-items: stretch;
    justify-content: space-between;
    gap: 14px;
  }

  .card.past {
    opacity: 0.72;
  }

  .card-main {
    display: flex;
    flex-direction: column;
    gap: 6px;
    min-width: 0;
  }

  .badges {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }

  .badge {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 2px 6px;
    border: 1.5px solid var(--ink);
  }

  .badge.new {
    background: var(--accent);
  }

  .badge.soon {
    background: var(--blue);
    color: var(--paper);
    border-color: var(--blue);
  }

  .badge.ref {
    background: transparent;
    color: var(--ink);
  }

  .title {
    font-size: 16px;
    font-weight: 700;
    line-height: 1.3;
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
  }

  .title:hover,
  .title:focus-visible {
    background: linear-gradient(transparent 60%, var(--accent) 60%);
    text-decoration-color: var(--ink);
  }

  .meta {
    margin: 0;
    font-size: 13px;
    color: var(--ink);
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    align-items: baseline;
  }

  .meta .dot {
    opacity: 0.5;
  }

  .card-side {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    justify-content: center;
    gap: 2px;
    text-align: right;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .close-date {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
  }

  .close-rel {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.04em;
    opacity: 0.7;
  }

  .close-rel.urgent {
    color: var(--blue);
    opacity: 1;
    font-weight: 700;
  }

  .empty {
    font-size: 14px;
    line-height: 1.5;
  }

  @media (max-width: 540px) {
    .card {
      flex-direction: column;
    }
    .card-side {
      align-items: flex-start;
      text-align: left;
      flex-direction: row;
      gap: 8px;
    }
  }
</style>
