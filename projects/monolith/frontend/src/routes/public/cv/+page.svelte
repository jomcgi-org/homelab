<script>
  import { onMount } from "svelte";
  import { Footer, Sticker, Marquee, Seo } from "$lib/public/components";
  import {
    contact,
    name,
    tagline,
    summary,
    jobs,
    earlierCareer,
    personalIntro,
    projects,
    skills,
  } from "./cv-data.js";

  // Minimal inline-markdown tokenizer. The CV bullets carry just two markdown
  // constructs from cv.md — **emphasis** and [text](url) — so a focused
  // tokenizer beats pulling in a full markdown dependency. Emphasis splits two
  // ways: a digit-bearing token mid-sentence is a metric (the only coral on
  // the page); everything else — including the lead-in phrase that opens a
  // bullet — renders as a plain bold run-in heading. Links render as inline
  // anchors.
  function tokenize(text) {
    const tokens = [];
    const re = /\*\*(.+?)\*\*|\[(.+?)\]\((.+?)\)/g;
    let last = 0;
    let m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) {
        tokens.push({ type: "text", text: text.slice(last, m.index) });
      }
      if (m[1] !== undefined) {
        const isMetric = /\d/.test(m[1]) && m.index > 0;
        tokens.push({ type: isMetric ? "em" : "lead", text: m[1] });
      } else {
        tokens.push({ type: "link", text: m[2], href: m[3] });
      }
      last = re.lastIndex;
    }
    if (last < text.length) {
      tokens.push({ type: "text", text: text.slice(last) });
    }
    return tokens;
  }

  // Identity ticker — deliberately NOT the skill list (that lives in the
  // chip grid of section 03). Keeping these distinct avoids showing the same
  // list twice; the marquee is a signature, the chips are the reference.
  const MARQUEE_ITEMS = [
    "Senior Platform Engineer @ Semgrep",
    "AWS / EKS",
    "eBPF & Cilium",
    "Distributed Systems",
    "OpenTelemetry Contributor",
    "K3S Homelab",
    "Vancouver, Canada",
  ];

  // Scroll-triggered reveals, mirroring the homepage's IntersectionObserver.
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

<Seo
  title="Joe McGinley · CV"
  description="Joe McGinley, Senior Platform Engineer @ Semgrep. AWS · EKS · Kubernetes · eBPF · Reliability & Observability."
  path="/cv"
  type="profile"
/>

{#snippet inline(text)}
  {#each tokenize(text) as tok}
    {#if tok.type === "em"}<span class="metric">{tok.text}</span
      >{:else if tok.type === "lead"}<span class="lead">{tok.text}</span
      >{:else if tok.type === "link"}<a
        class="inline-link"
        href={tok.href}
        target="_blank"
        rel="noreferrer">{tok.text}</a
      >{:else}{tok.text}{/if}
  {/each}
{/snippet}

{#snippet bandHead(num, title, meta)}
  <div class="band-head">
    <span class="band-num mono">{num}</span>
    <span class="band-title mono">{title}</span>
    {#if meta}
      <span class="band-meta mono">{meta}</span>
    {/if}
  </div>
{/snippet}

<div class="cv page">
  <!-- ═══ Identity hero (cream) ═══ -->
  <header class="band band--cream hero">
    <svg class="deco deco-star" width="52" height="52" viewBox="0 0 40 40"
      ><path
        d="M20,2 L22.5,14 L34,10 L26,20 L34,30 L22.5,26 L20,38 L17.5,26 L6,30 L14,20 L6,10 L17.5,14 Z"
        fill="var(--blue)"
        stroke="var(--ink)"
        stroke-width="2"
        stroke-linejoin="round"
      /></svg
    >
    <svg class="deco deco-diamond" width="20" height="20" viewBox="0 0 24 24"
      ><path
        d="M12,2 L22,12 L12,22 L2,12 Z"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2"
      /></svg
    >
    <svg class="deco deco-squiggle" width="76" height="22" viewBox="0 0 80 24"
      ><path
        d="M2,12 Q 10,2 18,12 T 34,12 T 50,12 T 66,12 T 78,12"
        fill="none"
        stroke="var(--ink)"
        stroke-width="2.5"
        stroke-linecap="round"
      /></svg
    >

    <div class="wrap-narrow hero-content">
      <p class="eyebrow">Curriculum Vitae</p>
      <h1 class="cv-name display">{name}</h1>
      <p class="cv-tagline mono">{tagline}</p>
      <div class="cv-contacts">
        <a class="btn btn-primary" href={`mailto:${contact.email}`}
          >{contact.email}</a
        >
        <a
          class="btn btn-secondary"
          href={contact.linkedin.href}
          target="_blank"
          rel="noreferrer">{contact.linkedin.label}</a
        >
        <a
          class="btn btn-secondary"
          href={contact.github.href}
          target="_blank"
          rel="noreferrer">{contact.github.label}</a
        >
        <span class="loc-chip mono">◍ {contact.location}</span>
      </div>
      <div class="summary-card">
        <span class="summary-tab mono">Profile</span>
        <p class="cv-summary">{@render inline(summary)}</p>
      </div>
      <Sticker color="var(--accent)" rotate={-4} class="hero-sticker"
        >BARE-METAL K3S</Sticker
      >
    </div>
  </header>

  <!-- ═══ Identity ticker (signature, not skills) ═══ -->
  <Marquee items={MARQUEE_ITEMS} />

  <!-- ═══ Work experience (paper) ═══ -->
  <section class="band band--paper reveal">
    <div class="wrap-narrow">
      {@render bandHead("01", "Work Experience")}
      <div class="jobs">
        {#each jobs as job}
          <article class="job">
            <div class="job-head">
              <div class="job-id">
                <h3 class="job-company">{job.company}</h3>
                <p class="job-role mono">{job.title}</p>
              </div>
              <span class="job-dates mono">{job.dates}</span>
            </div>
            {#if job.blurb}
              <p class="job-blurb">{@render inline(job.blurb)}</p>
            {/if}
            {#if job.bullets}
              <ul class="bullets">
                {#each job.bullets as bullet}
                  <li>{@render inline(bullet)}</li>
                {/each}
              </ul>
            {/if}
            {#if job.highlights}
              {#each job.highlights as highlight}
                <div class="highlight">
                  <h4 class="highlight-title mono">{highlight.title}</h4>
                  {#if highlight.kicker}
                    <p class="highlight-kicker">{highlight.kicker}</p>
                  {/if}
                  {#if highlight.intro}
                    <p class="highlight-intro">
                      {@render inline(highlight.intro)}
                    </p>
                  {/if}
                  <ul class="bullets">
                    {#each highlight.bullets as bullet}
                      <li>{@render inline(bullet)}</li>
                    {/each}
                  </ul>
                </div>
              {/each}
            {/if}
          </article>
        {/each}
      </div>
      <div class="earlier">
        <p class="eyebrow">Earlier Career</p>
        <p class="earlier-text">{@render inline(earlierCareer)}</p>
      </div>
    </div>
  </section>

  <!-- ═══ Personal projects (cream) ═══ -->
  <section class="band band--cream reveal">
    <div class="wrap-narrow">
      {@render bandHead("02", "Personal Engineering", "Homelab")}
      <p class="section-intro">{@render inline(personalIntro)}</p>
      <ul class="bullets bullets--lg">
        {#each projects as project}
          <li>{@render inline(project)}</li>
        {/each}
      </ul>
    </div>
  </section>

  <!-- ═══ Technical expertise (paper) ═══ -->
  <section class="band band--paper reveal">
    <div class="wrap-narrow">
      {@render bandHead("03", "Technical Expertise")}
      <div class="skills">
        {#each skills as cat}
          <div class="skill-cat">
            <h4 class="skill-label mono">{cat.label}</h4>
            <div class="chips">
              {#each cat.items as item}
                <span class="chip mono">{item}</span>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </div>
  </section>

  <Footer />
</div>

<style>
  .cv {
    background: var(--cream);
    color: var(--ink);
  }

  /* ── Full-bleed bands ─────────────────────── */
  .band {
    border-bottom: 2px solid var(--ink);
    padding: 48px 0;
    position: relative;
    overflow: hidden;
  }
  .band--cream {
    background: var(--cream);
  }
  .band--paper {
    background: var(--paper);
  }

  /* ── Hero ─────────────────────────────────── */
  .hero {
    padding: 64px 0 52px;
  }
  .hero-content {
    position: relative;
  }
  .cv-name {
    font-size: clamp(46px, 7.5vw, 88px);
    margin: 8px 0 12px;
  }
  .cv-tagline {
    font-size: 13px;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    margin-bottom: 22px;
  }
  .cv-contacts {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
    margin-bottom: 30px;
  }
  /* Contrasting profile card so the bio doesn't blend into the cream band.
     One accent treatment only: border + offset shadow + label tab. */
  .summary-card {
    position: relative;
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: 6px 6px 0 var(--ink);
    padding: 26px 30px 24px;
    max-width: 66ch;
  }
  .summary-tab {
    position: absolute;
    top: -13px;
    left: 24px;
    background: var(--ink);
    color: var(--paper);
    border: 2px solid var(--ink);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    padding: 3px 10px;
  }
  .cv-summary {
    font-family: var(--sans);
    font-size: clamp(16px, 1.4vw, 19px);
    line-height: 1.55;
    color: var(--ink);
  }
  .loc-chip {
    display: inline-flex;
    align-items: center;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.06em;
    padding: 13px 18px;
    border: 2px dashed var(--rule-2);
    color: var(--ink-2);
  }
  :global(.hero-sticker) {
    position: absolute;
    top: -8px;
    right: 0;
  }

  /* ── Decorative shapes (kept clear of the sticky nav) ─── */
  .deco {
    position: absolute;
    pointer-events: none;
  }
  .deco-star {
    top: 24px;
    right: max(20px, calc(50% - 520px));
    transform: rotate(-12deg);
  }
  .deco-diamond {
    bottom: 36px;
    left: max(20px, calc(50% - 520px));
  }
  .deco-squiggle {
    top: 40px;
    left: max(16px, calc(50% - 530px));
  }

  /* ── Chunky numbered section-header bar ───── */
  .band-head {
    display: flex;
    align-items: stretch;
    border: 2px solid var(--ink);
    background: var(--ink);
    color: var(--paper);
    box-shadow: 4px 4px 0 var(--ink);
    margin-bottom: 32px;
  }
  .band-num {
    background: var(--accent);
    color: var(--ink);
    font-weight: 700;
    font-size: 15px;
    letter-spacing: 0.05em;
    padding: 13px 16px;
    border-right: 2px solid var(--ink);
    display: flex;
    align-items: center;
  }
  .band-title {
    flex: 1;
    display: flex;
    align-items: center;
    padding: 13px 16px;
    font-weight: 700;
    font-size: 15px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
  }
  .band-meta {
    display: flex;
    align-items: center;
    padding: 13px 16px;
    font-size: 12px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--blue);
    border-left: 2px solid rgba(255, 255, 255, 0.25);
  }

  /* ── Job entries (flat, non-interactive) ──── */
  .jobs {
    display: flex;
    flex-direction: column;
  }
  .job {
    padding: 26px 0;
    border-bottom: 2px solid var(--rule);
  }
  .job:first-child {
    padding-top: 4px;
  }
  .job:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }
  .job-head {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 14px;
  }
  .job-company {
    font-family: var(--sans);
    font-size: 24px;
    font-weight: 700;
    line-height: 1.1;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .job-company::before {
    content: "";
    width: 13px;
    height: 13px;
    background: var(--ink);
    flex-shrink: 0;
  }
  .job-role {
    font-size: 12px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-top: 6px;
    margin-left: 23px;
  }
  .job-dates {
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.04em;
    color: var(--ink);
    white-space: nowrap;
    background: var(--accent);
    padding: 7px 12px;
    border: 2px solid var(--ink);
    box-shadow: 3px 3px 0 var(--ink);
  }

  /* ── Role blurb + named project subsections ─ */
  .job-blurb {
    font-family: var(--sans);
    font-size: 16px;
    line-height: 1.55;
    color: var(--ink-2);
    margin: 0 0 18px 23px;
  }
  .highlight {
    margin: 0 0 22px 23px;
  }
  .highlight:last-child {
    margin-bottom: 0;
  }
  .highlight-title {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink);
    margin-bottom: 6px;
  }
  .highlight-title::before {
    content: "";
    width: 10px;
    height: 10px;
    background: var(--accent);
    border: 2px solid var(--ink);
    flex-shrink: 0;
  }
  .highlight-kicker {
    font-family: var(--sans);
    font-size: 15px;
    font-weight: 600;
    line-height: 1.45;
    color: var(--ink);
    margin: 0 0 8px;
  }
  .highlight-intro {
    font-family: var(--sans);
    font-size: 14px;
    line-height: 1.55;
    color: var(--ink-3);
    margin: 0 0 12px;
  }
  .highlight .bullets {
    margin-left: 0;
  }

  /* ── Earlier career footnote ──────────────── */
  .earlier {
    margin-top: 28px;
    padding-top: 22px;
    border-top: 2px solid var(--rule);
  }
  .earlier-text {
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink-2);
    margin-top: 8px;
  }

  /* ── Section intro / aside lines ──────────── */
  .section-intro {
    font-family: var(--sans);
    font-size: 16px;
    line-height: 1.55;
    color: var(--ink-2);
    margin: 0 0 20px;
  }

  /* ── Bullets ──────────────────────────────── */
  .bullets {
    display: flex;
    flex-direction: column;
    gap: 11px;
    margin-left: 23px;
  }
  .bullets--lg {
    margin-left: 0;
    gap: 16px;
  }
  .bullets li {
    position: relative;
    padding-left: 24px;
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink-2);
  }
  .bullets--lg li {
    font-size: 16px;
  }
  .bullets li::before {
    content: "▸";
    position: absolute;
    left: 0;
    top: 0;
    color: var(--ink);
    font-size: 15px;
    font-weight: 700;
  }

  /* Bullet lead-ins are run-in headings: bold ink, no marker. Coral is
     reserved for .metric below, so the eye can find the numbers. */
  .lead {
    font-weight: 700;
    color: var(--ink);
  }

  /* Metrics are the ONLY coral on the page — coral means "this is the number
     that matters." Spans wrap just the metric token, so the marker stays a
     clean single rectangle; box-decoration-break keeps any rare wrap tidy. */
  .metric {
    font-weight: 700;
    color: var(--ink);
    background: linear-gradient(transparent 56%, var(--coral) 56%);
    padding: 0 3px;
    -webkit-box-decoration-break: clone;
    box-decoration-break: clone;
  }
  .inline-link {
    font-weight: 600;
    text-decoration: underline;
    text-decoration-color: var(--blue);
    text-decoration-thickness: 2px;
    text-underline-offset: 3px;
    transition: text-decoration-color 160ms ease;
  }
  .inline-link:hover {
    text-decoration-color: var(--coral);
  }

  /* ── Skills ───────────────────────────────── */
  .skills {
    display: flex;
    flex-direction: column;
    gap: 20px;
  }
  .skill-cat {
    display: grid;
    grid-template-columns: 220px 1fr;
    gap: 20px;
    align-items: start;
    padding-bottom: 20px;
    border-bottom: 2px solid var(--rule);
  }
  .skill-cat:last-child {
    border-bottom: none;
    padding-bottom: 0;
  }
  .skill-label {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink);
    padding-top: 6px;
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
  .chip {
    font-size: 12px;
    font-weight: 500;
    padding: 7px 12px;
    background: var(--cream);
    border: 2px solid var(--ink);
    box-shadow: 2px 2px 0 var(--ink);
  }

  /* ── Responsive ───────────────────────────── */
  @media (max-width: 768px) {
    .hero {
      padding: 40px 0 36px;
    }
    .band {
      padding: 36px 0;
    }
    .deco,
    :global(.hero-sticker) {
      display: none;
    }
    .band-num,
    .band-title,
    .band-meta {
      padding: 11px 12px;
      font-size: 12px;
    }
    .band-meta {
      display: none;
    }
    .job-head {
      flex-direction: column;
      gap: 10px;
    }
    .skill-cat {
      grid-template-columns: 1fr;
      gap: 10px;
    }
  }

  /* ── Print: flat paper résumé ─────────────── */
  @media print {
    .cv {
      background: var(--paper);
    }
    .deco,
    :global(.hero-sticker),
    :global(.marquee),
    :global(.footer) {
      display: none !important;
    }
    .band {
      padding: 18px 0;
      border-bottom: none;
    }
    .band-head,
    .job-dates,
    .chip {
      box-shadow: none !important;
    }
    .job {
      break-inside: avoid;
    }
  }
</style>
