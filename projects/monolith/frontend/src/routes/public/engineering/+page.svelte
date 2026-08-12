<script>
  import { onMount } from "svelte";
  import { Footer, Sticker, Marquee } from "$lib/public/components";
  import {
    intro,
    marqueeItems,
    categories,
    projects,
  } from "./engineering-data.js";
  import { diagrams } from "./diagrams/index.js";

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

  // The count sticker is derived from the roster so it can never drift.
  const stickers = [`${projects.length} Systems`, ...intro.stickers];
  const stickerColors = ["var(--accent)", "var(--blue)", "var(--coral)"];

  // Active section for the desktop scroll-spy rail.
  let activeId = $state("");

  // Rail anchor: vertical center of the viewport band BELOW the sticky
  // nav and the yellow ticker, so the rail never overlaps the ticker.
  // Recomputed on scroll; once the ticker leaves the viewport the band is
  // the full viewport under the nav and the rail sits at true center.
  let railY = $state(0);

  onMount(() => {
    // Scroll-triggered reveals, mirroring the CV page's IntersectionObserver.
    const reveal = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) {
            e.target.classList.add("in");
            reveal.unobserve(e.target);
          }
        }
      },
      { threshold: 0.12 },
    );
    for (const el of document.querySelectorAll(".reveal")) {
      reveal.observe(el);
    }

    // Scroll-spy: the rootMargin biases the active band to the top third
    // of the viewport, so a section counts as active while its content
    // occupies the band between 20% and 40% from the top.
    const spy = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) activeId = e.target.id;
        }
      },
      { rootMargin: "-20% 0px -60% 0px" },
    );
    for (const el of document.querySelectorAll("section.dive[id]")) {
      spy.observe(el);
    }

    const nav = document.querySelector(".md-nav");
    const marquee = document.querySelector(".marquee");
    const updateRail = () => {
      const navBottom = nav ? nav.getBoundingClientRect().bottom : 0;
      const marqueeBottom = marquee
        ? marquee.getBoundingClientRect().bottom
        : 0;
      const top = Math.max(0, navBottom, marqueeBottom);
      railY = top + (window.innerHeight - top) / 2;
    };
    updateRail();
    window.addEventListener("scroll", updateRail, { passive: true });
    window.addEventListener("resize", updateRail, { passive: true });

    return () => {
      reveal.disconnect();
      spy.disconnect();
      window.removeEventListener("scroll", updateRail);
      window.removeEventListener("resize", updateRail);
    };
  });
</script>

<svelte:head>
  <title>Joe McGinley · Engineering</title>
  <meta
    name="description"
    content="Engineering deep dives: agents, operators, data systems, and build tooling running on a bare-metal Kubernetes homelab."
  />
</svelte:head>

<div class="eng page">
  <!-- ═══ Hero ═══ -->
  <header class="hero">
    <div class="wrap hero-content">
      <h1 class="display eng-title">{intro.title}</h1>
      <div class="hero-stickers">
        {#each stickers as s, i}
          <Sticker
            color={stickerColors[i % stickerColors.length]}
            rotate={i % 2 ? 3 : -3}
          >
            {s}
          </Sticker>
        {/each}
        <a
          class="sticker-link"
          href={intro.source.href}
          target="_blank"
          rel="noreferrer"
        >
          <Sticker color="var(--paper)" rotate={2}>{intro.source.label}</Sticker
          >
        </a>
      </div>
      <p class="lede">{intro.lede}</p>
    </div>
  </header>

  <Marquee items={marqueeItems} />

  <!-- ═══ Numbered index (mobile) ═══ -->
  <nav class="wrap toc mono" aria-label="Section index">
    {#each numbered as p}
      <a class="toc-item" href={`#${p.id}`}>
        <span class="toc-num">{p.num}</span>
        <span>{p.title}</span>
      </a>
    {/each}
  </nav>

  <!-- ═══ Scroll-spy rail (desktop) ═══ -->
  <nav
    class="rail mono"
    aria-label="Sections"
    style:top={railY ? `${railY}px` : null}
  >
    {#each numbered as p}
      <a
        class="rail-link"
        href={`#${p.id}`}
        aria-current={activeId === p.id ? "true" : undefined}
      >
        <span class="rail-title">{p.title}</span>
        <span class="rail-num">{p.num}</span>
      </a>
    {/each}
  </nav>

  <!-- ═══ Deep dives ═══ -->
  <div class="wrap dives">
    {#each numbered as p}
      {@const Diagram = diagrams[p.id]}
      <section class="dive reveal" id={p.id} aria-labelledby={`${p.id}-h`}>
        <div class="dive-head">
          <span class="dive-num mono">{p.num}</span>
          <h2 class="dive-title" id={`${p.id}-h`}>
            {#if p.repoHref}
              <a
                class="title-link"
                href={p.repoHref}
                target="_blank"
                rel="noreferrer"
              >
                {p.title}<span class="title-arrow" aria-hidden="true">↗</span>
              </a>
            {:else}
              {p.title}
            {/if}
          </h2>
          <span class="tag mono">
            <span
              class="tag-dot"
              style:background={categories[p.category].color}
            ></span>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="tag mono tag-status">{p.status}</span>
          {/if}
          {#each p.extraLinks as l}
            <a
              class="tag mono tag-live"
              href={l.href}
              target={l.href.startsWith("/") ? undefined : "_blank"}
              rel={l.href.startsWith("/") ? undefined : "noreferrer"}
              aria-label={l.label}
              title={l.label}
            >
              Live ↗
            </a>
          {/each}
        </div>

        <div
          class="motivation"
          style:border-left-color={categories[p.category].color}
        >
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
      </section>
    {/each}
  </div>

  <Footer />
</div>

<style>
  /* ═══ Hero ═══ */
  .hero {
    padding: 48px 0 40px;
    border-bottom: 2px solid var(--ink);
    background: var(--cream);
  }

  .hero-content {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 18px;
  }

  .eng-title {
    font-size: clamp(52px, 7.5vw, 88px);
  }

  .hero-stickers {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
    align-items: center;
  }

  .sticker-link {
    text-decoration: none;
    transition: transform 120ms ease;
    display: inline-block;
  }

  .sticker-link:hover {
    transform: translate(-2px, -2px);
  }

  .lede {
    max-width: 560px;
    font-size: 18px;
    color: var(--ink-2);
  }

  /* ═══ Numbered index (mobile only) ═══ */
  .toc {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px 24px;
    padding-top: 28px;
    padding-bottom: 8px;
  }

  .toc-item {
    display: flex;
    gap: 10px;
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink-2);
    text-decoration: none;
  }

  .toc-item:hover {
    color: var(--ink);
    text-decoration: underline;
    text-underline-offset: 3px;
  }

  .toc-num {
    color: var(--ink-3);
    flex-shrink: 0;
  }

  @media (min-width: 1024px) {
    .toc {
      display: none;
    }
  }

  /* ═══ Scroll-spy rail (desktop only) ═══ */
  /* Fixed to the right edge, numbers matching the section headings' mono
     treatment. Hover or keyboard focus expands the rail leftward to show
     titles; the active section's number runs at full opacity. */
  .rail {
    display: none;
  }

  @media (min-width: 1024px) {
    .rail {
      position: fixed;
      right: 16px;
      top: 50%;
      transform: translateY(-50%);
      z-index: 40;
      display: flex;
      flex-direction: column;
      gap: 4px;
      align-items: flex-end;
      /* Chrome is invisible while collapsed; on expand the rail becomes a
         single hard-shadowed paper panel (the site's card language) so
         titles sit on one coherent surface instead of per-item chips
         that read as ragged blobs wherever they cross page content. */
      padding: 10px 12px;
      border: 2px solid transparent;
      border-radius: var(--radius);
      transition:
        background 140ms ease,
        border-color 140ms ease,
        box-shadow 140ms ease;
    }

    .rail:hover,
    .rail:focus-within {
      background: var(--paper);
      border-color: var(--ink);
      box-shadow: var(--shadow-hard);
    }
  }

  .rail-link {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    padding: 2px 6px;
    /* Transparent border reserves the space so the hover chip's ink
       border doesn't shift the row. */
    border: 2px solid transparent;
    border-radius: 4px;
    text-decoration: none;
    transition:
      background 120ms ease,
      border-color 120ms ease,
      box-shadow 120ms ease;
  }

  .rail-num {
    font-size: 13px;
    color: var(--ink);
    opacity: 0.25;
    transition: opacity 140ms ease;
  }

  /* Coral is the site's existing "active" accent (the nav underline), so
     the rail's current-section marker joins that thread. */
  .rail-link[aria-current="true"] .rail-num {
    opacity: 1;
    color: var(--coral);
    font-weight: 700;
  }

  .rail-link[aria-current="true"] .rail-title {
    color: var(--ink);
    font-weight: 700;
  }

  /* Hover/focus chip: blue fill, ink border, hard shadow, the sticker
     treatment. Declared after the aria-current rules so the ink text
     wins over the coral marker while the pointer is on the row. */
  .rail-link:hover,
  .rail-link:focus-visible {
    background: var(--blue);
    border-color: var(--ink);
    box-shadow: var(--shadow-hard-sm);
  }

  .rail-link:hover .rail-num,
  .rail-link:focus-visible .rail-num {
    opacity: 1;
    color: var(--ink);
  }

  .rail-link:hover .rail-title,
  .rail-link:focus-visible .rail-title {
    color: var(--ink);
  }

  .rail-title {
    max-width: 0;
    opacity: 0;
    overflow: hidden;
    white-space: nowrap;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-2);
    transition:
      max-width 180ms ease,
      opacity 140ms ease;
  }

  /* Expanded state: titles slide out leftward inside the panel. Wide
     enough for the longest title (Bazel: One Way to Build Everything)
     without clipping. */
  .rail:hover .rail-title,
  .rail:focus-within .rail-title {
    max-width: 320px;
    opacity: 1;
  }

  /* ═══ Tags (shared by index + dive headers) ═══ */
  /* Outline + colored dot: the category colour is a single small cue
     rather than a saturated block. */
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
     distinct treatment: a solid yellow sticker fill rather than the
     outlined category pill. */
  .tag-status {
    background: var(--accent);
  }

  /* Live destination chip: the heading owns the source link, this chip
     owns the running product (trips.jomcgi.dev, jomcgi.dev/app/ships, the
     notes view). Green = online; full label in title/aria-label. */
  .tag-live {
    background: var(--green);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease;
  }

  .tag-live:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard-sm);
  }

  /* ═══ Deep dives ═══ */
  .dives {
    display: flex;
    flex-direction: column;
    gap: 72px;
    padding-top: 48px;
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
     highlighter swipe wipes in behind the text and the arrow nudges, so
     it reads as text first and a link on intent. The band is a
     background (not a border), so it sits behind the glyphs and never
     floats below the serif descenders the way the old underline did. */
  .title-link {
    color: inherit;
    text-decoration: none;
    background-image: linear-gradient(var(--accent), var(--accent));
    background-repeat: no-repeat;
    background-position: 0 78%;
    background-size: 0% 0.4em;
    transition: background-size 220ms ease;
  }

  .title-link:hover {
    background-size: 100% 0.4em;
  }

  @media (prefers-reduced-motion: reduce) {
    .title-link {
      transition: none;
    }
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
    /* No fill: a cream fill here matches the page background and reads as
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
      padding-top: 36px;
    }
  }
</style>
