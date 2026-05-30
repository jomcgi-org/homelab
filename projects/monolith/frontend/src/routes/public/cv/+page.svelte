<script>
  import { onMount } from "svelte";
  import { Footer } from "$lib/public/components";
  import { contact, name, summary, jobs, projects, skills } from "./cv-data.js";

  // Minimal inline-markdown tokenizer. The CV bullets carry just two markdown
  // constructs from cv.md — **emphasis** and [text](url) — so a focused
  // tokenizer beats pulling in a full markdown dependency. Emphasis renders as
  // a coral-underlined metric; links render as inline anchors.
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
        tokens.push({ type: "em", text: m[1] });
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

<svelte:head>
  <title>Joe McGinley — CV</title>
  <meta
    name="description"
    content="Joe McGinley — Senior Platform Engineer. GCP · Kubernetes · Go · Python · Reliability & Observability."
  />
</svelte:head>

<div class="cv page">
  <!-- ═══ Identity hero ═══ -->
  <header class="cv-hero">
    <!-- decorative shapes, echoing the homepage gutter motifs -->
    <svg class="deco deco-diamond" width="22" height="22" viewBox="0 0 24 24"
      ><path d="M12,2 L22,12 L12,22 L2,12 Z" fill="none" stroke="var(--ink)" stroke-width="2" /></svg
    >
    <svg class="deco deco-star" width="48" height="48" viewBox="0 0 40 40"
      ><path
        d="M20,2 L22.5,14 L34,10 L26,20 L34,30 L22.5,26 L20,38 L17.5,26 L6,30 L14,20 L6,10 L17.5,14 Z"
        fill="var(--blue)"
        stroke="var(--ink)"
        stroke-width="2"
        stroke-linejoin="round"
      /></svg
    >
    <svg class="deco deco-circle" width="20" height="20" viewBox="0 0 24 24"
      ><circle cx="12" cy="12" r="10" fill="var(--coral)" stroke="var(--ink)" stroke-width="2" /></svg
    >

    <div class="wrap-narrow hero-content">
      <p class="eyebrow">Curriculum Vitae</p>
      <h1 class="cv-name display">{name}</h1>
      <p class="cv-tagline mono">Senior Platform Engineer · GCP · Kubernetes · Reliability</p>
      <p class="cv-summary">{summary}</p>
      <div class="cv-contacts">
        <a class="btn btn-primary" href={`mailto:${contact.email}`}>{contact.email}</a>
        <a class="btn btn-secondary" href={contact.linkedin.href} target="_blank" rel="noreferrer"
          >{contact.linkedin.label}</a
        >
        <a class="btn btn-secondary" href={contact.github.href} target="_blank" rel="noreferrer"
          >{contact.github.label}</a
        >
        <span class="loc-chip mono">◍ {contact.location}</span>
      </div>
    </div>
  </header>

  <main class="wrap-narrow cv-body">
    <!-- ═══ Work experience ═══ -->
    <section class="cv-section reveal">
      <h2 class="section-title display">Work Experience</h2>
      <div class="jobs">
        {#each jobs as job}
          <article class="card-hard job">
            <div class="job-head">
              <div class="job-id">
                <h3 class="job-company">{job.company}</h3>
                <p class="job-title">{job.title}</p>
              </div>
              <span class="job-dates mono">{job.dates}</span>
            </div>
            <ul class="bullets">
              {#each job.bullets as bullet}
                <li>
                  {#each tokenize(bullet) as tok}
                    {#if tok.type === "em"}<span class="metric">{tok.text}</span
                      >{:else if tok.type === "link"}<a
                        class="inline-link"
                        href={tok.href}
                        target="_blank"
                        rel="noreferrer">{tok.text}</a
                      >{:else}{tok.text}{/if}
                  {/each}
                </li>
              {/each}
            </ul>
          </article>
        {/each}
      </div>
    </section>

    <!-- ═══ Personal projects ═══ -->
    <section class="cv-section reveal">
      <h2 class="section-title display">Personal Projects</h2>
      <article class="card-hard projects-card">
        <ul class="bullets">
          {#each projects as project}
            <li>
              {#each tokenize(project) as tok}
                {#if tok.type === "em"}<span class="metric">{tok.text}</span
                  >{:else if tok.type === "link"}<a
                    class="inline-link"
                    href={tok.href}
                    target="_blank"
                    rel="noreferrer">{tok.text}</a
                  >{:else}{tok.text}{/if}
              {/each}
            </li>
          {/each}
        </ul>
      </article>
    </section>

    <!-- ═══ Technical expertise ═══ -->
    <section class="cv-section reveal">
      <h2 class="section-title display">Technical Expertise</h2>
      <div class="skills">
        {#each skills as cat}
          <div class="skill-cat">
            <h3 class="skill-label mono">{cat.label}</h3>
            <div class="chips">
              {#each cat.items as item}
                <span class="chip">{item}</span>
              {/each}
            </div>
          </div>
        {/each}
      </div>
    </section>
  </main>

  <Footer />
</div>

<style>
  .cv {
    background: var(--cream);
    color: var(--ink);
  }

  /* ── Hero ─────────────────────────────────── */
  .cv-hero {
    position: relative;
    overflow: hidden;
    padding: 72px 0 56px;
    border-bottom: 2px solid var(--ink);
  }

  .hero-content {
    position: relative;
  }

  .cv-name {
    font-size: clamp(48px, 8vw, 92px);
    margin: 6px 0 10px;
  }

  .cv-tagline {
    font-size: 13px;
    letter-spacing: 0.04em;
    color: var(--ink-3);
    margin-bottom: 22px;
  }

  .cv-summary {
    font-family: var(--sans);
    font-size: clamp(17px, 1.6vw, 21px);
    line-height: 1.55;
    color: var(--ink-2);
    max-width: 60ch;
    margin-bottom: 30px;
  }

  .cv-contacts {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    align-items: center;
  }

  .loc-chip {
    display: inline-flex;
    align-items: center;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.06em;
    padding: 12px 18px;
    border: 2px dashed var(--rule-2);
    color: var(--ink-2);
  }

  /* ── Decorative shapes (gutter, outside content box) ─── */
  .deco {
    position: absolute;
    pointer-events: none;
  }
  .deco-diamond {
    top: 64px;
    left: max(16px, calc(50% - 520px));
  }
  .deco-star {
    top: 56px;
    right: max(16px, calc(50% - 520px));
    transform: rotate(-12deg);
  }
  .deco-circle {
    bottom: 48px;
    right: max(24px, calc(50% - 500px));
  }

  /* ── Body ─────────────────────────────────── */
  .cv-body {
    padding: 56px 32px 80px;
  }

  .cv-section + .cv-section {
    margin-top: 56px;
  }

  .section-title {
    font-size: clamp(30px, 4vw, 44px);
    margin-bottom: 24px;
    padding-bottom: 10px;
    border-bottom: 2px solid var(--ink);
  }

  /* ── Job cards ────────────────────────────── */
  .jobs {
    display: flex;
    flex-direction: column;
    gap: 22px;
  }

  .job {
    padding: 24px 26px;
  }

  .job-head {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 16px;
    flex-wrap: wrap;
    margin-bottom: 14px;
    padding-bottom: 12px;
    border-bottom: 1.5px solid var(--rule);
  }

  .job-company {
    font-family: var(--sans);
    font-size: 22px;
    font-weight: 600;
    line-height: 1.1;
  }

  .job-title {
    font-family: var(--sans);
    font-size: 15px;
    color: var(--ink-3);
    margin-top: 2px;
  }

  .job-dates {
    font-size: 12px;
    font-weight: 600;
    letter-spacing: 0.04em;
    color: var(--ink-2);
    white-space: nowrap;
    background: var(--accent);
    padding: 5px 10px;
    border: 1.5px solid var(--ink);
  }

  /* ── Bullets ──────────────────────────────── */
  .bullets {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .bullets li {
    position: relative;
    padding-left: 22px;
    font-family: var(--sans);
    font-size: 15px;
    line-height: 1.55;
    color: var(--ink-2);
  }

  .bullets li::before {
    content: "▸";
    position: absolute;
    left: 0;
    top: 0;
    color: var(--coral);
    font-size: 14px;
  }

  .projects-card {
    padding: 24px 26px;
  }

  /* Emphasized metrics — coral underline, echoing the homepage hero links */
  .metric {
    font-weight: 600;
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 3px;
  }

  .inline-link {
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
    gap: 22px;
  }

  .skill-label {
    font-size: 12px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 10px;
  }

  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  .chip {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 500;
    padding: 7px 12px;
    background: var(--paper);
    border: 1.5px solid var(--ink);
    transition:
      transform 120ms ease,
      box-shadow 120ms ease,
      background 120ms ease;
  }

  .chip:hover {
    transform: translate(-2px, -2px);
    box-shadow: 2px 2px 0 var(--ink);
    background: var(--blue);
  }

  /* ── Responsive ───────────────────────────── */
  @media (max-width: 768px) {
    .cv-hero {
      padding: 48px 0 40px;
    }
    .cv-body {
      padding: 40px 20px 64px;
    }
    .job {
      padding: 20px;
    }
    .deco {
      display: none;
    }
    .job-head {
      flex-direction: column;
      align-items: flex-start;
      gap: 8px;
    }
  }

  /* ── Print: flatten shadows/colours for a clean paper resume ─── */
  @media print {
    .cv {
      background: var(--paper);
    }
    .deco {
      display: none;
    }
    .card-hard {
      box-shadow: none !important;
      transform: none !important;
      break-inside: avoid;
    }
    .job-dates {
      background: transparent;
    }
    :global(.footer) {
      display: none;
    }
  }
</style>
