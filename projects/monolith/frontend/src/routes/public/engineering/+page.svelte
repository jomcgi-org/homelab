<script>
  import { onMount } from "svelte";
  import { Footer, Marquee } from "$lib/public/components";
  import { intro, marqueeItems, categories, projects } from "./engineering-data.js";
  import { diagrams } from "./diagrams/index.js";

  // The systems count drives both the meta line and the first stat cell,
  // derived from the roster so it can never drift from the real number.
  const metaLine = `${projects.length} systems · ${intro.metaTail}`;
  const stats = [{ value: String(projects.length), label: "systems" }, ...intro.stats];

  // Stable two-digit section numbers derived from roster order. The repo
  // link is split out from the rest: it moves onto the section heading
  // (so the title itself is the link to the source), leaving only genuine
  // live destinations (trips.jomcgi.dev, /notes, ...) as bottom buttons.
  const numbered = projects.map((p, i) => {
    const links = p.links ?? [];
    const repo = links.find((l) => l.href.includes("github.com"));
    return {
      ...p,
      num: String(i + 1).padStart(2, "0"),
      repoHref: repo ? repo.href : null,
      extraLinks: links.filter((l) => l !== repo),
    };
  });

  // Scroll-triggered reveals, mirroring the CV page's IntersectionObserver.
  onMount(() => {
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            observer.unobserve(e.target);
          }
        }
      },
      { threshold: 0.12 },
    );
    for (const el of document.querySelectorAll(".reveal")) {
      observer.observe(el);
    }
    return () => observer.disconnect();
  });
</script>

<svelte:head>
  <title>Joe McGinley — Engineering</title>
  <meta
    name="description"
    content="Engineering deep dives: agents, operators, data systems, and build tooling running on a bare-metal Kubernetes homelab."
  />
</svelte:head>

<div class="eng page">
  <!-- ═══ Hero ═══ -->
  <header class="hero">
    <div class="wrap hero-grid">
      <div class="hero-lead">
        <h1 class="display eng-title">{intro.title}</h1>
        <p class="meta-line mono">{metaLine}</p>
        <p class="lede">{intro.lede}</p>
        <a class="btn source-btn" href={intro.source.href} target="_blank" rel="noreferrer">
          {intro.source.label}
        </a>
      </div>
      <div class="hero-stats" aria-hidden="true">
        {#each stats as s}
          <div class="stat" class:accent={s.accent}>
            <span class="stat-val" class:small={s.small}>{s.value}</span>
            <span class="stat-label mono">{s.label}</span>
          </div>
        {/each}
      </div>
    </div>
  </header>

  <Marquee items={marqueeItems} />

  <!-- ═══ Expo grid ═══ -->
  <section class="wrap expo" aria-label="Project index">
    {#each numbered as p, i}
      <a class="card-hard expo-card reveal" class:d1={i % 3 === 1} class:d2={i % 3 === 2} href={`#${p.id}`}>
        <div class="expo-card-top">
          <span class="expo-num mono">{p.num}</span>
          <span class="tag mono">
            <span class="tag-dot" style:background={categories[p.category].color}></span>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="tag mono tag-status">{p.status}</span>
          {/if}
        </div>
        <h2 class="expo-title">{p.title}</h2>
        <p class="expo-liner">{p.oneLiner}</p>
        <span class="expo-more mono">Deep dive →</span>
      </a>
    {/each}
  </section>

  <!-- ═══ Deep dives ═══ -->
  <div class="wrap dives">
    {#each numbered as p}
      {@const Diagram = diagrams[p.id]}
      <section class="dive reveal" id={p.id} aria-labelledby={`${p.id}-h`}>
        <div class="dive-head">
          <span class="dive-num mono">{p.num}</span>
          <h2 class="dive-title" id={`${p.id}-h`}>
            {#if p.repoHref}
              <a class="title-link" href={p.repoHref} target="_blank" rel="noreferrer">
                {p.title}<span class="title-arrow" aria-hidden="true">↗</span>
              </a>
            {:else}
              {p.title}
            {/if}
          </h2>
          <span class="tag mono">
            <span class="tag-dot" style:background={categories[p.category].color}></span>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="tag mono tag-status">{p.status}</span>
          {/if}
        </div>

        <div class="motivation" style:border-left-color={categories[p.category].color}>
          <span class="motivation-label mono">Motivation</span>
          <p>{p.motivation}</p>
        </div>

        {#if Diagram}
          <Diagram />
        {/if}

        <dl class="facts">
          {#each p.facts as f}
            <dt class="mono">{f.k}</dt>
            <dd>{f.v}</dd>
          {/each}
        </dl>

        {#if p.snippet}
          <pre class="snippet mono"><code>{p.snippet.code}</code></pre>
        {/if}

        {#if p.extraLinks.length}
          <div class="dive-links">
            {#each p.extraLinks as l}
              <a
                class="btn source-btn"
                href={l.href}
                target={l.href.startsWith("/") ? undefined : "_blank"}
                rel={l.href.startsWith("/") ? undefined : "noreferrer"}
              >
                {l.label}
              </a>
            {/each}
          </div>
        {/if}
      </section>
    {/each}
  </div>

  <Footer />
</div>

<style>
  /* ═══ Hero ═══ */
  /* Two columns: lead on the left, a stat block on the right so the
     upper-right of the fold has a job instead of reading as empty cream.
     Tightened vertical rhythm pulls the expo grid closer to the fold. */
  .hero {
    padding: 52px 0 44px;
    border-bottom: 2px solid var(--ink);
    background: var(--cream);
  }

  .hero-grid {
    display: grid;
    grid-template-columns: 1.25fr 1fr;
    gap: 48px;
    align-items: center;
  }

  .hero-lead {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 16px;
  }

  .eng-title {
    font-size: clamp(56px, 8vw, 92px);
  }

  /* Calm monospace meta line in place of the rotated sticker cluster, so
     it doesn't compete with the yellow ticker for the same register. */
  .meta-line {
    font-size: 13px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-3);
  }

  .lede {
    max-width: 560px;
    font-size: 18px;
    color: var(--ink-2);
  }

  .hero-stats {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }

  .stat {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    box-shadow: var(--shadow-hard-sm);
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }

  .stat.accent {
    background: var(--accent);
  }

  .stat-val {
    font-family: var(--serif);
    font-size: 40px;
    line-height: 1;
  }

  .stat-val.small {
    font-family: var(--mono);
    font-size: 15px;
    font-weight: 700;
    line-height: 1.45;
  }

  .stat-label {
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-top: 2px;
  }

  @media (max-width: 720px) {
    .hero-grid {
      grid-template-columns: 1fr;
      gap: 28px;
    }
  }

  /* Paper fill so the source link reads as a button instead of ghosting
     into the cream background as a bare outline. Reused by the live-site
     buttons in the deep dives. */
  .source-btn {
    background: var(--paper);
  }

  /* ═══ Tags (shared by cards + dive headers) ═══ */
  /* Outline + colored dot rather than a saturated fill: keeps the
     category colour-coding as a single small cue and drops the block of
     colour that dominated the index (and fixed the low-contrast dark
     operators tag, since text is always ink on white). */
  .tag {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink);
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 3px 8px;
    border-radius: 4px;
    white-space: nowrap;
  }

  .tag-dot {
    width: 8px;
    height: 8px;
    border-radius: 999px;
    border: 1.5px solid var(--ink);
    flex-shrink: 0;
  }

  /* Maturity (PRE-ALPHA) is a different axis from category, so it gets a
     distinct treatment: a solid yellow fill rather than the outlined
     category pill, so the two never read as the same kind of label. */
  .tag-status {
    background: var(--accent);
  }

  /* ═══ Expo grid ═══ */
  .expo {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 18px;
    padding-top: 44px;
    padding-bottom: 44px;
  }

  .expo-card {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding: 18px;
    text-decoration: none;
  }

  /* Override card-hard's default lift with a press: on hover the whole
     card (it is the anchor) pushes down-right into its own shadow, which
     collapses. The hard-shadow style makes this read as a physical click
     target. */
  .expo-card:hover,
  .expo-card:focus-visible {
    transform: translate(4px, 4px);
    box-shadow: none;
  }

  .expo-card-top {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 2px;
  }

  .expo-num {
    font-size: 12px;
    color: var(--ink-3);
    margin-right: auto;
  }

  .expo-title {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 23px;
    line-height: 1.05;
  }

  .expo-liner {
    font-size: 14px;
    line-height: 1.45;
    color: var(--ink-2);
    flex-grow: 1;
  }

  .expo-more {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-top: 4px;
  }

  /* ═══ Deep dives ═══ */
  .dives {
    display: flex;
    flex-direction: column;
    gap: 72px;
    padding-bottom: 96px;
  }

  .dive {
    scroll-margin-top: 80px;
    display: flex;
    flex-direction: column;
    gap: 22px;
  }

  .dive-head {
    display: flex;
    align-items: baseline;
    gap: 14px;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 12px;
    flex-wrap: wrap;
  }

  .dive-num {
    font-size: 13px;
    color: var(--ink-3);
  }

  .dive-title {
    font-family: var(--serif);
    font-weight: 400;
    font-size: clamp(30px, 4.5vw, 44px);
    line-height: 1;
  }

  /* The heading itself is the repo link. Plain until hover, then a
     hard underline + the arrow nudges, so it reads as text first and a
     link on intent. */
  .title-link {
    color: inherit;
    text-decoration: none;
    border-bottom: 2px solid transparent;
    transition: border-color 140ms ease;
  }

  .title-link:hover {
    border-bottom-color: var(--ink);
  }

  .title-arrow {
    font-family: var(--mono);
    font-size: 0.5em;
    vertical-align: super;
    margin-left: 4px;
    color: var(--ink-3);
    transition: transform 140ms ease;
    display: inline-block;
  }

  .title-link:hover .title-arrow {
    transform: translate(2px, -2px);
    color: var(--ink);
  }

  .dive-head .tag {
    align-self: center;
  }

  /* Paper card with a thick category-coloured left rule, replacing the
     solid ink block (eleven dark slabs down the page read as heavy). The
     left rule keeps one calm thread of the category colour. */
  .motivation {
    background: var(--paper);
    border: 2px solid var(--ink);
    border-left-width: 6px;
    border-radius: var(--radius);
    padding: 16px 20px;
    box-shadow: var(--shadow-hard-sm);
  }

  .motivation-label {
    display: block;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 6px;
  }

  .motivation p {
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink-2);
  }

  .facts {
    display: grid;
    grid-template-columns: 180px 1fr;
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    overflow: hidden;
    background: var(--paper);
  }

  .facts dt {
    padding: 12px 16px;
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    border-bottom: 1px solid var(--rule);
    border-right: 2px solid var(--ink);
    /* No fill: the old cream fill matched the page background and read as
       a punched-out hole. The bold mono weight and the vertical ink rule
       carry the key/value separation instead. */
    background: var(--paper);
  }

  .facts dd {
    padding: 12px 16px;
    font-size: 14px;
    color: var(--ink-2);
    border-bottom: 1px solid var(--rule);
  }

  .facts dt:nth-last-of-type(1),
  .facts dd:nth-last-of-type(1) {
    border-bottom: none;
  }

  .snippet {
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    background: var(--paper);
    box-shadow: var(--shadow-hard-sm);
    padding: 16px 18px;
    font-size: 13px;
    overflow-x: auto;
  }

  .dive-links {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
  }

  @media (max-width: 720px) {
    .facts {
      grid-template-columns: 1fr;
    }
    /* In one column the last dd is the true last element; the last dt
       still needs its rule to separate key from value. Repeating the
       nth-last-of-type selector here outranks the base border removal. */
    .facts dt,
    .facts dt:nth-last-of-type(1) {
      border-right: none;
      border-bottom: 1px solid var(--rule);
    }
    .dives {
      gap: 56px;
    }
  }
</style>
