<script>
  import {
    Seo,
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import {
    intro,
    marqueeItems,
    categories,
    projects,
  } from "./engineering-data.js";
  import { diagrams } from "./diagrams/index.js";

  // Stable two-digit record numbers derived from roster order, the same
  // numbers the spine index shows. The repo link is split out from the
  // rest so the head band can name it as the record's source; genuine live
  // destinations (trips, ships, the notes view) sit beside it.
  const numbered = projects.map((p, i) => {
    const links = p.links ?? [];
    const repo = links.find((l) => l.href.includes("github.com"));
    return {
      ...p,
      num: String(i + 1).padStart(2, "0"),
      repo: repo ?? null,
      extraLinks: links.filter((l) => l !== repo),
    };
  });

  const external = (href) => !href.startsWith("/");

  // The spine follows the reader: the topmost record in the upper band of
  // the viewport is the active one, as on the blog index.
  let activeId = $state("");

  $effect(() => {
    const records = document.querySelectorAll("[data-system]");
    if (!records.length) return;
    const observer = new IntersectionObserver(
      (observed) => {
        const visible = observed
          .filter((item) => item.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) activeId = visible[0].target.dataset.system;
      },
      { rootMargin: "-15% 0px -65% 0px" },
    );
    records.forEach((record) => observer.observe(record));
    return () => observer.disconnect();
  });
</script>

<Seo
  title="Engineering · jomcgi.dev"
  description="Engineering deep dives: agents, operators, data systems, and build tooling running on a bare-metal Kubernetes homelab."
  path="/engineering"
/>

{#snippet trail()}
  <Trail crumbs={[{ label: "engineering" }]} />
{/snippet}

<TechnicalDrawingChrome {trail} />

<main class="td sheet eng-page">
  <div class="frame">
    <h1 class="sr-only">{intro.title}</h1>

    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">
          {@render trail()}
        </div>

        <nav class="index strip" aria-label="Systems">
          <p class="sec-label">/ Index</p>
          <div class="index-list">
            {#each numbered as p (p.id)}
              <a
                href={`#${p.id}`}
                class:active={activeId === p.id}
                aria-current={activeId === p.id ? "location" : undefined}
              >
                <span class="num">{p.num}</span>
                <span>{p.title}</span>
              </a>
            {/each}
          </div>
        </nav>

        <SiteIndex here="engineering" />
      </aside>

      <div class="content">
        <section class="edition intro" aria-labelledby="intro-h">
          <div class="ed-head">
            <span><span class="num">{projects.length}</span> systems</span>
            <span class="end">
              <a href={intro.source.href} target="_blank" rel="noreferrer"
                >{intro.source.label}</a
              >
            </span>
          </div>
          <div class="ed-lead">
            <h2 id="intro-h">{intro.title}</h2>
            <p>{intro.lede}</p>
          </div>
          <ul class="divided" aria-label="Stack">
            {#each marqueeItems as item (item)}
              <li>{item}</li>
            {/each}
          </ul>
        </section>

        <div class="editions" aria-label="Deep dives">
          {#each numbered as p (p.id)}
            {@const Diagram = diagrams[p.id]}
            <article
              class="edition system"
              id={p.id}
              data-system={p.id}
              aria-labelledby={`${p.id}-h`}
            >
              <div class="ed-head">
                <span class="num">{p.num}</span>
                <span>{categories[p.category].label}</span>
                {#if p.status}
                  <span>{p.status}</span>
                {/if}
                <span class="end">
                  {#each p.extraLinks as l (l.href)}
                    <a
                      href={l.href}
                      target={external(l.href) ? "_blank" : undefined}
                      rel={external(l.href) ? "noreferrer" : undefined}
                      >{l.label}{external(l.href) ? " ↗" : ""}</a
                    >
                  {/each}
                  {#if p.repo}
                    <a href={p.repo.href} target="_blank" rel="noreferrer"
                      >{p.repo.label} ↗</a
                    >
                  {/if}
                </span>
              </div>

              <div class="ed-lead">
                <h2 id={`${p.id}-h`}>{p.title}</h2>
                <p>{p.oneLiner}</p>
              </div>

              <div class="ed-body prose">
                <h3>Motivation</h3>
                <p>{p.motivation}</p>

                {#if Diagram}
                  <Diagram />
                {/if}

                <dl class="spec">
                  {#each p.facts as f (f.k)}
                    <div class="row">
                      <dt>{f.k}</dt>
                      <dd>{f.v}</dd>
                    </div>
                  {/each}
                </dl>

                {#if p.snippet}
                  <pre class="doc-code" data-lang={p.snippet.lang}><code
                      >{p.snippet.code}</code
                    ></pre>
                {/if}
              </div>
            </article>
          {/each}
        </div>
      </div>
    </div>
  </div>
</main>

<style>
  @media (prefers-reduced-motion: no-preference) {
    :global(html) {
      scroll-behavior: smooth;
    }
  }

  /* Figures count across the whole page, so each record's drawing carries
     the record's own number. */
  .editions {
    counter-reset: fig;
  }

  .editions .prose {
    counter-reset: none;
  }

  .intro {
    margin-bottom: 1.5rem;
  }

  /* A snippet that closes a record ends on the record's border. */
  .prose > pre.doc-code:last-child {
    border-bottom: 0;
  }

  @media (max-width: 760px) {
    /* The sticky index strip sits over the top of the page, so a record
       jumped to from it lands below the strip rather than under it. */
    .system {
      scroll-margin-top: 3.2rem;
    }
  }
</style>
