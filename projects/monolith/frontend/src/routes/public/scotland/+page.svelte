<script>
  import {
    Seo,
    SiteIndex,
    TechnicalDrawingChrome,
    Trail,
  } from "$lib/public/components";
  import "$lib/public/styles/technical-drawing.css";
  import {
    METHOD_URL,
    SPEC_URL,
    curl,
    facts,
    listings,
    setups,
    sources,
  } from "./scotland-data.js";

  const sections = [
    { id: "connect", num: "01", label: "Connect" },
    { id: "api", num: "02", label: "API" },
    { id: "data", num: "03", label: "Data" },
  ];
</script>

<Seo
  title="Scotland connector · jomcgi.dev"
  description="A free MCP server and REST API that helps an assistant plan and check Scottish trips: which walks fit the weather and where the night sky will be dark and clear."
  path="/scotland"
/>

{#snippet trail()}
  <Trail crumbs={[{ label: "scotland" }]} />
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
            {#each sections as s (s.id)}
              <a href={`#${s.id}`}>
                <span class="num">{s.num}</span>
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
            <span class="end">v1 · read-only</span>
          </div>
          <div class="ed-lead">
            <h1 id="intro-h">
              Ask your assistant what to do outdoors, and whether it is on.
            </h1>
            <p>
              Plan: give it a place in Scotland and a few days, and for each day
              it suggests the Walkhighlands walks that fit the weather and the
              best nearby dark-sky site for the night, each with go or marginal
              and the reason, plus sunrise, sunset and the moon.
            </p>
            <p>
              Check: name a walk and a day, or a place and a night, and it says
              whether it is on, when to start, which other days suit it and what
              is better nearby. Each walk links to its route on Walkhighlands.
            </p>
          </div>
          <dl class="spec">
            {#each facts as f (f.k)}
              <div class="row">
                <dt>{f.k}</dt>
                <dd class:mono={f.code}>{f.v}</dd>
              </div>
            {/each}
          </dl>
          <div class="ed-foot">
            <a href="/app/stars">prefer a map? the stars app</a>
          </div>
        </section>

        <section class="edition" id="connect" aria-labelledby="connect-h">
          <div class="ed-head">
            <span class="num">01</span>
            <span>Connect</span>
          </div>
          <div class="ed-lead">
            <h2 id="connect-h">Add it to your assistant</h2>
            <p>It is a remote MCP server: one URL, no account.</p>
          </div>
          <div class="ed-body prose">
            {#each setups as s (s.id)}
              <h3>{s.label}</h3>
              {#if s.how}
                <p>{s.how}</p>
              {/if}
              {#if s.code}
                <pre class="doc-code" data-lang={s.lang}><code>{s.code}</code
                  ></pre>
              {/if}
            {/each}
          </div>
          <ul class="divided listings" aria-label="Listings">
            {#each listings as l (l.label)}
              <li>
                {#if l.href}
                  <a href={l.href} target="_blank" rel="noreferrer"
                    >{l.label} ↗</a
                  >
                {:else}
                  <span>{l.label}</span> <small>soon</small>
                {/if}
              </li>
            {/each}
          </ul>
        </section>

        <section class="edition" id="api" aria-labelledby="api-h">
          <div class="ed-head">
            <span class="num">02</span>
            <span>API</span>
          </div>
          <div class="ed-lead">
            <h2 id="api-h">Or call it over HTTPS</h2>
            <p>
              The same answers as JSON, cached at the edge. Every response
              carries when the forecast was fetched and the sources to credit.
            </p>
          </div>
          <div class="ed-body prose">
            <pre class="doc-code" data-lang="sh"><code>{curl}</code></pre>
          </div>
          <div class="ed-foot">
            <a href={SPEC_URL}>OpenAPI spec</a>
            <a href={METHOD_URL}>method</a>
          </div>
        </section>

        <section class="edition" id="data" aria-labelledby="data-h">
          <div class="ed-head">
            <span class="num">03</span>
            <span>Data</span>
          </div>
          <div class="ed-lead">
            <h2 id="data-h">Where the answers come from</h2>
          </div>
          <dl class="spec">
            {#each sources as s (s.name)}
              <div class="row">
                <dt>{s.name}</dt>
                <dd>{s.use} · {s.licence}</dd>
              </div>
            {/each}
          </dl>
        </section>
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

  /* A snippet that opens a panel docks to the panel's rule, and one that
     closes it above a link row shares that row's rule. */
  .prose > pre.doc-code:first-child {
    margin-top: 0;
    border-top: 0;
  }

  .prose:has(+ .ed-foot) > pre.doc-code:last-child {
    border-bottom: 0;
  }

  .ed-lead h1 {
    font-size: clamp(1.5rem, 3vw, 2rem);
  }

  .mono {
    font-family: var(--font-code);
    font-size: 0.8rem;
    overflow-wrap: anywhere;
  }

  /* The strip sits under a snippet's own rule, so it neither overlaps it
     nor draws a second, fainter one. */
  .divided.listings {
    margin-top: 0;
  }

  .divided.listings > li {
    border-top: 0;
  }

  .listings small {
    font-family: var(--font-code);
    color: var(--ink-2);
  }

  @media (max-width: 760px) {
    .edition[id] {
      scroll-margin-top: 3.2rem;
    }
  }
</style>
