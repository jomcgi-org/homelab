<script>
  import { onMount } from "svelte";
  import { Footer, Sticker, Marquee } from "$lib/public/components";
  import { intro, marqueeItems, categories, projects } from "./engineering-data.js";
  import { diagrams } from "./diagrams/index.js";

  // Stable two-digit section numbers derived from roster order.
  const numbered = projects.map((p, i) => ({
    ...p,
    num: String(i + 1).padStart(2, "0"),
  }));

  const stickerColors = ["var(--accent)", "var(--blue)", "var(--coral)"];

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
    <div class="wrap hero-content">
      <p class="eyebrow">{intro.eyebrow}</p>
      <h1 class="display eng-title">{intro.title}</h1>
      <div class="hero-stickers">
        {#each intro.stickers as s, i}
          <Sticker color={stickerColors[i % stickerColors.length]} rotate={i % 2 ? 3 : -3}>
            {s}
          </Sticker>
        {/each}
      </div>
      <p class="lede">{intro.lede}</p>
      <a class="btn btn-secondary" href={intro.source.href} target="_blank" rel="noreferrer">
        {intro.source.label}
      </a>
    </div>
  </header>

  <Marquee items={marqueeItems} />

  <!-- ═══ Expo grid ═══ -->
  <section class="wrap expo" aria-label="Project index">
    {#each numbered as p, i}
      <a class="card-hard expo-card reveal" class:d1={i % 3 === 1} class:d2={i % 3 === 2} href={`#${p.id}`}>
        <div class="expo-card-top">
          <span class="expo-num mono">{p.num}</span>
          <span class="expo-tag mono" style:background={categories[p.category].color}>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="expo-tag mono expo-status">{p.status}</span>
          {/if}
        </div>
        <h2 class="expo-title">{p.title}</h2>
        <p class="expo-liner">{p.oneLiner}</p>
        <span class="expo-more mono">Deep dive ↓</span>
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
          <h2 class="dive-title" id={`${p.id}-h`}>{p.title}</h2>
          <span class="dive-tag mono" style:background={categories[p.category].color}>
            {categories[p.category].label}
          </span>
          {#if p.status}
            <span class="dive-tag mono dive-status">{p.status}</span>
          {/if}
        </div>

        <div class="motivation">
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

        {#if p.links?.length}
          <div class="dive-links">
            {#each p.links as l}
              <a
                class="btn btn-secondary"
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
  .hero {
    padding: 72px 0 56px;
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
    font-size: clamp(56px, 10vw, 120px);
  }

  .hero-stickers {
    display: flex;
    gap: 14px;
    flex-wrap: wrap;
  }

  .lede {
    max-width: 560px;
    font-size: 18px;
    color: var(--ink-2);
  }

  /* ═══ Expo grid ═══ */
  .expo {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 24px;
    padding-top: 56px;
    padding-bottom: 56px;
  }

  .expo-card {
    display: flex;
    flex-direction: column;
    gap: 10px;
    padding: 20px;
  }

  .expo-card-top {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .expo-num {
    font-size: 12px;
    color: var(--ink-3);
    margin-right: auto;
  }

  .expo-tag {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 2px solid var(--ink);
    padding: 3px 8px;
    border-radius: 4px;
  }

  .expo-status {
    background: var(--paper);
  }

  .expo-title {
    font-family: var(--serif);
    font-weight: 400;
    font-size: 26px;
    line-height: 1.05;
  }

  .expo-liner {
    font-size: 14px;
    color: var(--ink-2);
    flex-grow: 1;
  }

  .expo-more {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
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

  .dive-tag {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 2px solid var(--ink);
    padding: 3px 8px;
    border-radius: 4px;
    align-self: center;
  }

  .dive-status {
    background: var(--paper);
  }

  .motivation {
    background: var(--ink);
    color: var(--cream);
    border-radius: var(--radius);
    padding: 18px 20px;
    box-shadow: var(--shadow-hard-sm);
  }

  .motivation-label {
    display: block;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    opacity: 0.7;
    margin-bottom: 6px;
  }

  .motivation p {
    font-size: 15px;
    line-height: 1.55;
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
    background: var(--cream);
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
