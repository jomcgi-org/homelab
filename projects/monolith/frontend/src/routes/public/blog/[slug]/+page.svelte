<script>
  import { Seo } from "$lib/public/components";
  import { formatDate } from "../blog.js";

  let { data } = $props();
</script>

<Seo
  title={`${data.title} · jomcgi.dev`}
  description={data.summary}
  path={`/blog/${data.slug}`}
  type="article"
/>

<main class="td post-page">
  <div class="post-frame">
    <article class="edition">
      <p class="ed-head">
        <time datetime={data.date}>{formatDate(data.date)}</time>
        <a href="/blog">Index</a>
      </p>

      <header class="ed-lead">
        <h1>{data.title}</h1>
        <p>{data.summary}</p>
      </header>

      <div class="meta-rows">
        <p class="meta-kv">
          <span class="k">Filed</span>
          <span class="v filed-tags">
            {#each data.tags as tag}
              <a href={`/blog?tag=${encodeURIComponent(tag)}`}>{tag}</a>
            {/each}
          </span>
        </p>
      </div>

      <!-- Server-rendered, constrained first-party markdown. -->
      <div class="post-body">{@html data.html}</div>
    </article>
  </div>
</main>

<style>
  .post-page {
    box-sizing: border-box;
    min-height: 100vh;
    padding: clamp(3.25rem, 5vh, 3.75rem) clamp(1.35em, 5vw, 4.5em) 6em;
    background: var(--sheet);
    color: var(--ink);
    font-family: var(--font-ui);
    font-size: 16px;
  }

  .post-page a:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }

  .post-frame {
    max-width: 48em;
    margin: 0 auto;
  }

  .edition {
    border: 1px solid var(--ink);
  }

  .ed-head {
    display: flex;
    justify-content: space-between;
    margin: 0;
    padding: 0.55em 1rem;
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .ed-head time,
  .ed-head a {
    color: var(--ink-2);
  }

  .ed-head a {
    text-decoration: none;
  }

  .ed-head a:hover {
    color: var(--accent-ink);
  }

  .ed-lead {
    padding: 1rem;
  }

  .ed-lead h1 {
    max-width: 40ch;
    margin: 0;
    font-size: clamp(1.25rem, 2.5vw, 1.6rem);
    font-weight: 700;
    letter-spacing: -0.025em;
    line-height: 1.15;
  }

  .ed-lead p {
    margin: 0.6rem 0 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.55;
  }

  .meta-rows {
    border-top: 1px solid var(--stroke);
  }

  .meta-kv {
    display: grid;
    grid-template-columns: 6.5em minmax(0, 1fr);
    align-items: stretch;
    margin: 0;
  }

  .meta-kv .k {
    padding: 0.5em 0 0.5em 1rem;
  }

  .meta-kv .v {
    border-left: 1px solid var(--line);
  }

  .meta-kv .k,
  .meta-kv .v {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .filed-tags {
    display: flex;
    flex-wrap: wrap;
    overflow: hidden;
  }

  .filed-tags a {
    box-sizing: border-box;
    flex: 0 0 10.5rem;
    margin: -1px 0 0 -1px;
    padding: 0.5em 0.7em;
    border-top: 1px solid var(--line);
    border-left: 1px solid var(--line);
    color: var(--ink-2);
    text-decoration: none;
  }

  .filed-tags a:hover {
    color: var(--accent-ink);
  }

  .post-body {
    padding: 0 1rem 1rem;
    border-top: 1px solid var(--stroke);
    counter-reset: fig;
  }

  .post-body :global(h1),
  .post-body :global(h2),
  .post-body :global(h3),
  .post-body :global(h4),
  .post-body :global(h5),
  .post-body :global(h6) {
    color: var(--ink);
    scroll-margin-top: 4rem;
  }

  .post-body :global(h1),
  .post-body :global(h2) {
    margin: 1.4rem -1rem 0.85rem;
    padding: 0.45em 1rem;
    border-top: 1px solid var(--stroke);
    border-bottom: 1px solid var(--stroke);
    background: var(--band);
    font-family: var(--font-ui);
    font-size: 1.1rem;
    font-weight: 700;
    letter-spacing: -0.015em;
    line-height: 1.2;
  }

  .post-body :global(h3) {
    margin: 1.35rem 0 0.55rem;
    font-family: var(--font-ui);
    font-size: 0.95rem;
    font-weight: 700;
    line-height: 1.25;
  }

  .post-body :global(h4),
  .post-body :global(h5),
  .post-body :global(h6) {
    margin: 1.2rem 0 0.45rem;
    font-family: var(--font-code);
    font-size: 0.82rem;
    font-weight: 600;
    line-height: 1.3;
  }

  .post-body :global(p) {
    margin: 0.85rem 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.6;
  }

  .post-body :global(a) {
    color: var(--accent-ink);
    text-decoration: underline;
    text-underline-offset: 0.18em;
  }

  .post-body :global(strong) {
    font-weight: 650;
  }

  .post-body :global(ul),
  .post-body :global(ol) {
    margin: 0.85rem 0;
    padding-left: 1.5rem;
  }

  .post-body :global(li) {
    margin: 0.25rem 0;
    color: var(--ink);
    font-size: 0.92rem;
    line-height: 1.55;
  }

  .post-body :global(blockquote) {
    margin: 1rem 0;
    padding-left: 1rem;
    border-left: 2px solid var(--stroke);
    color: var(--ink-2);
  }

  .post-body :global(blockquote p) {
    color: inherit;
  }

  .post-body :global(hr) {
    margin: 1.5rem 0;
    border: 0;
    border-top: 1px solid var(--stroke);
  }

  .post-body :global(code) {
    padding: 0.12em 0.3em;
    background: var(--band);
    font-family: var(--font-code);
    font-size: 0.85em;
  }

  .post-body :global(pre.doc-code) {
    margin: 1rem 0;
    padding: 1rem;
    overflow-x: auto;
    border: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.8rem;
    line-height: 1.55;
  }

  .post-body :global(pre.doc-code code) {
    padding: 0;
    background: none;
    color: inherit;
    font-size: inherit;
  }

  .post-body :global(table) {
    width: 100%;
    margin: 1rem 0;
    border-collapse: collapse;
  }

  .post-body :global(th) {
    padding: 0.5em 0.7em;
    border-top: 1px solid var(--line);
    border-bottom: 1px solid var(--line);
    background: var(--band);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-align: left;
    text-transform: uppercase;
  }

  .post-body :global(td) {
    padding: 0.55em 0.7em;
    border-bottom: 1px solid var(--line);
    color: var(--ink);
    font-size: 0.85rem;
    line-height: 1.45;
    text-align: left;
    vertical-align: top;
  }

  .post-body :global(th + th),
  .post-body :global(td + td) {
    border-left: 1px solid var(--line);
  }

  .post-body :global(img) {
    display: block;
    max-width: 100%;
    height: auto;
  }

  .post-body :global(.fig) {
    margin: 1.5rem 0;
    border: 1px solid var(--stroke);
    counter-increment: fig;
  }

  .post-body :global(.fig-art) {
    padding: 1rem;
    overflow-x: auto;
    color: var(--ink);
  }

  /* A keyed figure is drawn for a 48em measure. Below that it scrolls
     sideways inside its panel instead of shrinking its 11px labels
     past legibility. */
  .post-body :global(.fig-art svg) {
    display: block;
    width: 100%;
    min-width: 36em;
    height: auto;
  }

  .post-body :global(.fig figcaption) {
    padding: 0.45em 1rem;
    border-top: 1px solid var(--stroke);
    background: var(--band);
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.72rem;
  }

  .post-body :global(.fig figcaption::before) {
    content: "Fig. " counter(fig) "  ";
  }

  .post-body :global(figure.fig + table) {
    margin-top: -1px;
  }

  .post-body :global(figure.fig + table th:first-child),
  .post-body :global(figure.fig + table td:first-child) {
    width: 3.5em;
    font-family: var(--font-code);
    text-align: right;
  }

  @media (max-width: 600px) {
    .post-page {
      padding-right: 1em;
      padding-left: 1em;
    }
  }
</style>
