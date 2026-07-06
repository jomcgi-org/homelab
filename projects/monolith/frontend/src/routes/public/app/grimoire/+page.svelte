<script>
  // Public Grimoire homepage: the pitch, how the pipeline works, what exists
  // today, a small invented demo of the grant system, and the roadmap. This
  // page is deliberately STATIC: it makes zero /api/grimoire fetches and
  // contains no corpus content, which is what lets the layout render it
  // outside the Turnstile gate (the gate protects the copyrighted corpus,
  // not the product description). Keep it that way: any corpus read added
  // here must move behind the gate instead.
  import { libraryHref, entitiesHref } from "$lib/public/grimoire/api.js";

  const pipeline = [
    {
      title: "Sourcebook PDF",
      body: "Marker extraction plus structural chunking: a monster's lore, stat block, and actions land in one chunk, in reading order.",
    },
    {
      title: "Postgres + embeddings",
      body: "Chunks land in Postgres keyed by book and section, and every chunk is embedded (pgvector) for semantic lookup.",
    },
    {
      title: "LLM entity extraction",
      body: "A frontier model reads each chunk and extracts typed entities, the chunks that mention them, and how they relate.",
    },
    {
      title: "Reader + entity browser",
      body: "Read a book chunk by chunk, or start from a creature or spell and jump straight to the passages it came from.",
    },
  ];

  const features = [
    {
      title: "The Library",
      body: "Every loaded book, grouped by kind (adventures, bestiaries, spellbooks, and more) with per-book chunk, image, and entity counts.",
    },
    {
      title: "Structural reader",
      body: "Continuous reading in source order with the section hierarchy and art preserved, not a wall of OCR text.",
    },
    {
      title: "Adventures",
      body: "Anthologies are split into their individual adventures, each with its own roster of the entities that appear in it.",
    },
    {
      title: "Entity browser",
      body: "Creatures, spells, NPCs, locations, items, and more as typed detail cards: stat blocks, spell levels, filter by type, search by name.",
    },
    {
      title: "EXPLORE canvas",
      body: "An interactive relationship graph: pick a scope (the whole corpus, one book, one adventure) and a lens (world, story, quests, rules), then wander from entity to entity.",
    },
    {
      title: "Provenance",
      body: "Every entity links back to the exact chunks it was extracted from, so a claim is always one click from its source text.",
    },
    {
      title: "Campaign grants",
      body: "In the private tier a DM controls what each player character knows about an entity, from full detail down to name-only recognition.",
    },
  ];

  const roadmap = [
    {
      status: "Planned",
      title: "Evidence-grounded verification",
      body: "A trailing job re-checks every extracted stat against the source passages, correcting what it can ground and nulling what it cannot.",
    },
    {
      status: "Planned",
      title: "Alias merge",
      body: 'Split-name twins ("Gundren" and "Gundren Rockseeker") get merged into one entity, report-first with a human approving every pair.',
    },
    {
      status: "Designed",
      title: "One search everywhere",
      body: "A single omnibox blending instant name matches with semantic hits over lore chunks and related entities.",
    },
    {
      status: "Designed",
      title: "Live-play tools",
      body: "DM advice capture and a live session view, so the grimoire is useful mid-encounter and not just between sessions.",
    },
    {
      status: "Long term",
      title: "Loom migration",
      body: "The schema is deliberately kept compatible with a governed lakehouse system of record, with per-session hot-tier checkouts when it lands.",
    },
  ];
</script>

<div class="home">
  <section class="hero">
    <p class="eyebrow">The Grimoire</p>
    <h1 class="grim-title hero-title">
      Your sourcebooks, <span class="accent">read and understood.</span>
    </h1>
    <p class="lede">
      Grimoire ingests D&amp;D sourcebook PDFs, chunks them along their real
      section structure, and has an LLM extract a typed knowledge graph of
      creatures, spells, NPCs, locations, and items. The result is a library
      you can read cover to cover and a corpus you can query entity by
      entity, with every fact traceable back to the page it came from.
    </p>
    <div class="cta-row">
      <a class="cta cta-primary" href={libraryHref()}>Browse the library</a>
      <a class="cta cta-ghost" href={entitiesHref()}>Explore entities</a>
    </div>
  </section>

  <section class="block">
    <div class="gh"><span class="kind">How it works</span></div>
    <ol class="pipeline">
      {#each pipeline as step, i (step.title)}
        <li class="step">
          <span class="step-n">{i + 1}</span>
          <h3 class="step-title">{step.title}</h3>
          <p class="step-body">{step.body}</p>
        </li>
      {/each}
    </ol>
    <p class="block-note">
      Ingest runs as idempotent batch jobs: re-loading an unchanged book is a
      no-op, and extraction prompts are versioned so the corpus can be
      re-extracted deliberately, never accidentally.
    </p>
  </section>

  <section class="block">
    <div class="gh"><span class="kind">What's here today</span></div>
    <ul class="feature-grid">
      {#each features as f (f.title)}
        <li class="feature">
          <h3 class="feature-title">{f.title}</h3>
          <p class="feature-body">{f.body}</p>
        </li>
      {/each}
    </ul>
  </section>

  <section class="block">
    <div class="gh">
      <span class="kind">At the table</span>
      <span class="kn">demo</span>
    </div>
    <p class="block-lede">
      Knowledge is granted, not global. The same entity renders differently
      for each player character depending on the scope their DM has granted.
      The creature below is invented for this demo; the real corpus works the
      same way.
    </p>
    <div class="grant-row">
      <article class="grant-card">
        <p class="scope scope-full">full</p>
        <h3 class="grim-title card-name">Vellum Lurker</h3>
        <p class="card-type">Medium aberration</p>
        <p class="card-stats">AC 15 <span class="dot">/</span> HP 66 <span class="dot">/</span> CR 4</p>
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
    <p class="block-note">
      One visibility predicate implements this everywhere: entity lookups,
      search results, and the reader's "entities on this page" chips all
      project through the same grant check.
    </p>
  </section>

  <section class="block">
    <div class="gh"><span class="kind">Roadmap</span></div>
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
    The library itself sits behind a quick human check: the corpus is
    copyrighted source material, so the reader is link-shareable but kept out
    of search engines and away from bots.
  </p>
</div>

<style>
  .home {
    max-width: 1180px;
    margin: 0 auto;
    padding: 56px 28px 80px;
  }

  .eyebrow {
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--grim-text-faint);
    font-weight: 600;
    margin: 0;
  }

  .hero-title {
    font-size: clamp(34px, 6vw, 54px);
    margin: 10px 0 0;
    max-width: 22ch;
  }

  .accent {
    color: var(--grim-accent);
  }

  .lede {
    margin-top: 18px;
    max-width: 68ch;
    color: var(--grim-text-dim);
    font-size: 15.5px;
    line-height: 1.65;
  }

  .cta-row {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 26px;
  }

  .cta {
    display: inline-flex;
    align-items: center;
    min-height: 42px;
    padding: 8px 18px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    text-decoration: none;
    border-radius: 7px;
  }

  .cta-primary {
    background: var(--grim-accent);
    color: var(--grim-on-accent);
  }

  .cta-primary:hover {
    background: var(--grim-accent-strong);
  }

  .cta-ghost {
    color: var(--grim-accent);
    border: 1px solid var(--grim-line);
  }

  .cta-ghost:hover {
    background: var(--grim-surface-2);
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

  /* ── Pipeline diagram: numbered step cards joined by arrows ── */

  .pipeline {
    list-style: none;
    margin: 22px 0 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 26px;
  }

  .step {
    position: relative;
    background: var(--grim-surface);
    border: 1px solid var(--grim-line);
    border-radius: 9px;
    padding: 16px 16px 18px;
  }

  .step:not(:last-child)::after {
    content: "\2192";
    position: absolute;
    top: 50%;
    right: -21px;
    transform: translateY(-50%);
    color: var(--grim-text-faint);
    font-size: 15px;
  }

  .step-n {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 22px;
    height: 22px;
    border-radius: 50%;
    background: var(--grim-accent-soft);
    color: var(--grim-accent);
    font-size: 11px;
    font-weight: 700;
    font-variant-numeric: tabular-nums;
  }

  .step-title {
    margin: 10px 0 0;
    font-family: var(--grim-serif);
    font-size: 16px;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .step-body {
    margin: 7px 0 0;
    color: var(--grim-text-dim);
    font-size: 12.5px;
    line-height: 1.55;
  }

  /* ── Feature grid ── */

  .feature-grid {
    list-style: none;
    margin: 22px 0 0;
    padding: 0;
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 14px;
  }

  .feature {
    background: var(--grim-surface);
    border: 1px solid var(--grim-line-soft);
    border-radius: 9px;
    padding: 16px;
  }

  .feature-title {
    margin: 0;
    font-family: var(--grim-serif);
    font-size: 16px;
    font-weight: 600;
    color: var(--grim-ink);
  }

  .feature-body {
    margin: 7px 0 0;
    color: var(--grim-text-dim);
    font-size: 12.5px;
    line-height: 1.55;
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
    .pipeline {
      grid-template-columns: 1fr 1fr;
    }

    .step:not(:last-child)::after {
      content: none;
    }

    .feature-grid,
    .grant-row {
      grid-template-columns: 1fr 1fr;
    }
  }

  @media (max-width: 640px) {
    .home {
      padding: 36px 20px 60px;
    }

    .pipeline,
    .feature-grid,
    .grant-row {
      grid-template-columns: 1fr;
    }

    .roadmap-row {
      grid-template-columns: 1fr;
      gap: 6px;
    }
  }
</style>
