<script>
  // Public Grimoire homepage: a scroll-scrubbed "From scan to query" explainer
  // (ScrollStory) over a small invented demo of the grant system and the
  // roadmap. This page still makes zero /api/grimoire
  // fetches, which is what lets the layout render it outside the Turnstile gate
  // (the gate protects the copyrighted corpus, not the product description).
  // The one deliberate exception: ScrollStory embeds ONE Joe-approved curated
  // corpus page (Lost Mine of Phandelver p.50) as baked static assets to show
  // the pipeline on real data. That single excerpt is the only corpus content
  // allowed out here; any further corpus read must still move behind the gate.
  import ScrollStory from "$lib/public/grimoire/scrollstory/ScrollStory.svelte";

  // The feature grid that used to live here was removed once the scroll story
  // shipped: the story demonstrates the library, reader, entities, graph, and
  // chat directly, so a prose retelling below it was redundant. The grant demo
  // and roadmap stay: they cover what the story cannot show (the private tier
  // and what is coming).
  const roadmap = [
    {
      status: "Planned",
      title: "Evidence-grounded verification",
      body: "Every stat gets re-checked against the passage it came from, and fixed or removed if it does not match.",
    },
    {
      status: "Planned",
      title: "Alias merge",
      body: 'Split-name twins ("Gundren" and "Gundren Rockseeker") get merged into one, with a person approving every pair.',
    },
    {
      status: "Designed",
      title: "One search everywhere",
      body: "One search box that finds a name as you type, and also finds the passages that describe it.",
    },
    {
      status: "Designed",
      title: "Live-play tools",
      body: "Notes and a live view to run a session from.",
    },
    {
      status: "Long term",
      title: "Loom migration",
      body: "Move the library onto Loom, so campaign data lives in one place with a record of every change.",
    },
  ];
</script>

<svelte:head>
  <title
    >Grimoire: a D&D campaign manager where each player sees only what the DM
    shares · jomcgi.dev</title
  >
  <meta
    name="description"
    content="Scan a sourcebook, then ask it questions and get answers with the page they came from. Every player sees only what their DM has shared."
  />
  <!-- Only this landing page is crawlable: every other route under
       /public/app/grimoire stays noindex (see the layout's svelte:head). -->
  <meta name="robots" content="index, follow" />
</svelte:head>

<ScrollStory />

<div class="home">
  <section class="block">
    <div class="gh">
      <h2 class="kind">At the table</h2>
      <span class="kn">demo</span>
    </div>
    <p class="block-lede">
      Each player sees the same creature differently, depending on how much
      their DM has shared. The creature below is made up for this demo; real
      ones work the same way.
    </p>
    <div class="grant-row">
      <article class="grant-card">
        <p class="scope scope-full">full</p>
        <h3 class="grim-title card-name">Vellum Lurker</h3>
        <p class="card-type">Medium aberration</p>
        <p class="card-stats">
          AC 15 <span class="dot">/</span> HP 66 <span class="dot">/</span> CR 4
        </p>
        <p class="card-body">
          A predator that folds itself flat between the pages of unattended
          books. Vulnerable to fire; regenerates in libraries.
        </p>
      </article>
      <article class="grant-card">
        <p class="scope scope-partial">partial</p>
        <h3 class="grim-title card-name">Vellum Lurker</h3>
        <p class="card-type">Medium aberration</p>
        <p class="card-stats redacted" aria-label="hidden stats">
          AC &#9612;&#9612; <span class="dot">/</span> HP &#9612;&#9612;
          <span class="dot">/</span> CR &#9612;&#9612;
        </p>
        <p class="card-body">
          A predator that folds itself flat between the pages of unattended
          books. <span class="redacted-run" aria-label="hidden text"
            >&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;&#9612;</span
          >
        </p>
      </article>
      <article class="grant-card">
        <p class="scope scope-name">name only</p>
        <h3 class="grim-title card-name">Vellum Lurker</h3>
        <p class="card-body card-body-faint">
          You recognise the name. Nothing more.
        </p>
      </article>
    </div>
  </section>

  <section class="block">
    <div class="gh"><h2 class="kind">Roadmap</h2></div>
    <ul class="roadmap">
      {#each roadmap as item (item.title)}
        <li class="roadmap-row">
          <span
            class="status"
            class:status-planned={item.status === "Planned"}
            class:status-designed={item.status === "Designed"}
            class:status-long={item.status === "Long term"}>{item.status}</span
          >
          <span class="roadmap-text">
            <span class="roadmap-title">{item.title}</span>
            <span class="roadmap-body">{item.body}</span>
          </span>
        </li>
      {/each}
    </ul>
  </section>

  <p class="foot-note">
    The library is free to browse. The interactive tools sit behind a quick
    human check to keep bots out.
  </p>
</div>

<style>
  .home {
    max-width: 1180px;
    margin: 0 auto;
    padding: 56px 28px 80px;
  }

  .block {
    margin-top: 56px;
  }

  .gh {
    display: flex;
    align-items: baseline;
    gap: 10px;
    padding: 0 8px 8px;
    border-bottom: 1px solid var(--grim-line);
  }

  .gh .kind {
    margin: 0;
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--grim-accent);
    font-weight: 700;
  }

  .gh .kn {
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
  }

  .block-lede {
    margin: 18px 8px 0;
    max-width: 68ch;
    color: var(--grim-text-dim);
    font-size: 14px;
    line-height: 1.6;
  }

  .block-note {
    margin: 20px 8px 0;
    max-width: 68ch;
    color: var(--grim-text-faint);
    font-size: 12.5px;
    line-height: 1.6;
  }

  /* ── Grant demo cards ── */

  .grant-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
    margin-top: 20px;
  }

  .grant-card {
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    padding: 16px;
  }

  .scope {
    display: inline-block;
    margin: 0 0 10px;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    border-radius: 4px;
    padding: 3px 7px;
  }

  .scope-full {
    color: var(--grim-type-location);
    background: color-mix(in srgb, var(--grim-type-location) 12%, transparent);
  }

  .scope-partial {
    color: var(--grim-type-npc);
    background: color-mix(in srgb, var(--grim-type-npc) 12%, transparent);
  }

  .scope-name {
    color: var(--grim-text-faint);
    background: var(--grim-surface-2);
  }

  .card-name {
    margin: 0;
    font-size: 19px;
  }

  .card-type {
    margin: 3px 0 0;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--grim-type-creature);
    font-weight: 600;
  }

  .card-stats {
    margin: 10px 0 0;
    font-size: 12.5px;
    color: var(--grim-ink);
    font-variant-numeric: tabular-nums;
  }

  .card-stats .dot,
  .card-body .dot {
    color: var(--grim-text-faint);
    margin: 0 6px;
  }

  .redacted,
  .redacted-run {
    color: var(--grim-text-faint);
    opacity: 0.55;
    letter-spacing: 0.05em;
  }

  .card-body {
    margin: 10px 0 0;
    color: var(--grim-text-dim);
    font-size: 12.5px;
    line-height: 1.55;
  }

  .card-body-faint {
    color: var(--grim-text-faint);
    font-style: italic;
  }

  /* ── Roadmap ── */

  .roadmap {
    list-style: none;
    margin: 8px 0 0;
    padding: 0;
  }

  .roadmap-row {
    display: grid;
    grid-template-columns: 92px 1fr;
    gap: 16px;
    align-items: start;
    padding: 15px 8px;
    border-bottom: 1px solid var(--grim-line-soft);
  }

  .roadmap-row:last-child {
    border-bottom: 0;
  }

  .status {
    justify-self: start;
    font-size: 9px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    border-radius: 4px;
    padding: 3px 7px;
    margin-top: 2px;
    white-space: nowrap;
  }

  .status-planned {
    color: var(--grim-accent);
    background: var(--grim-accent-soft);
  }

  .status-designed {
    color: var(--grim-type-spell);
    background: color-mix(in srgb, var(--grim-type-spell) 12%, transparent);
  }

  .status-long {
    color: var(--grim-text-faint);
    background: var(--grim-surface-2);
  }

  .roadmap-title {
    display: block;
    font-family: var(--grim-serif);
    font-size: 16px;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .roadmap-body {
    display: block;
    margin-top: 4px;
    color: var(--grim-text-dim);
    font-size: 12.5px;
    line-height: 1.55;
  }

  .foot-note {
    margin: 48px 8px 0;
    max-width: 68ch;
    color: var(--grim-text-faint);
    font-size: 12px;
    line-height: 1.6;
  }

  /* ── Responsive ── */

  @media (max-width: 960px) {
    .grant-row {
      grid-template-columns: 1fr 1fr;
    }
  }

  @media (max-width: 640px) {
    .home {
      padding: 36px 20px 60px;
    }

    .grant-row {
      grid-template-columns: 1fr;
    }

    .roadmap-row {
      grid-template-columns: 1fr;
      gap: 6px;
    }
  }
</style>
