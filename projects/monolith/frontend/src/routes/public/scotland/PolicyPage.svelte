<script>
  // One sheet for the connector's privacy, terms and support pages, in the
  // landing page's technical-drawing layout. The copy lives in
  // policy-data.js.
  import {
    Seo,
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import { UPDATED, policyLinks } from "./policy-data.js";

  let { doc } = $props();

  const num = (i) => String(i + 1).padStart(2, "0");
  const others = $derived(
    policyLinks.filter((l) => l.href !== `/scotland/${doc.slug}`),
  );
</script>

<Seo
  title={`${doc.title} · Scotland connector · jomcgi.dev`}
  description={doc.description}
  path={`/scotland/${doc.slug}`}
/>

{#snippet trail()}
  <Trail
    crumbs={[{ label: "scotland", href: "/scotland" }, { label: doc.slug }]}
  />
{/snippet}

<TechnicalDrawingChrome {trail} />

<main class="td sheet scotland-page">
  <div class="frame">
    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">
          {@render trail()}
        </div>

        <nav class="index strip" aria-label="Sections">
          <p class="sec-label">/ Index</p>
          <div class="index-list">
            {#each doc.sections as s, i (s.id)}
              <a href={`#${s.id}`}>
                <span class="num">{num(i)}</span>
                <span>{s.label}</span>
              </a>
            {/each}
          </div>
        </nav>

        <SiteIndex />
      </aside>

      <div class="content editions">
        <section class="edition" aria-labelledby="intro-h">
          <div class="ed-head">
            <span>Scotland connector</span>
            <span class="end">updated {UPDATED}</span>
          </div>
          <div class="ed-lead">
            <h1 id="intro-h">{doc.title}</h1>
            <p>{doc.lead}</p>
          </div>
          <div class="ed-foot">
            <a href="/scotland">the connector</a>
            {#each others as l (l.href)}
              <a href={l.href}>{l.label}</a>
            {/each}
          </div>
        </section>

        {#each doc.sections as s, i (s.id)}
          <section class="edition" id={s.id} aria-labelledby={`${s.id}-h`}>
            <div class="ed-head">
              <span class="num">{num(i)}</span>
              <span>{s.label}</span>
            </div>
            <div class="ed-lead">
              <h2 id={`${s.id}-h`}>{s.heading}</h2>
            </div>
            <div class="ed-body prose">
              {#each s.paras as p, j (j)}
                <p>{p}</p>
              {/each}
            </div>
            {#if s.rows}
              <dl class="spec">
                {#each s.rows as r (r.k)}
                  <div class="row">
                    <dt>{r.k}</dt>
                    <dd><a href={r.href}>{r.v}</a></dd>
                  </div>
                {/each}
              </dl>
            {/if}
          </section>
        {/each}
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

  .ed-lead h1 {
    font-size: clamp(1.5rem, 3vw, 2rem);
  }

  @media (max-width: 760px) {
    .edition[id] {
      scroll-margin-top: 3.2rem;
    }
  }
</style>
