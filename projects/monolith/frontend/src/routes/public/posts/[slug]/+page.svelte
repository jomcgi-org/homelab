<script>
  import { Seo } from "$lib/public/components";

  let { data } = $props();
</script>

<Seo
  title={`${data.title} · jomcgi.dev`}
  description={data.summary}
  path={`/posts/${data.slug}`}
  type="article"
/>

<main class="post page">
  <article class="wrap-narrow">
    <header>
      <h1 class="display">{data.title}</h1>
      <time datetime={data.date}>{data.date}</time>
    </header>

    <!-- Server-rendered, sanitised first-party markdown. -->
    <div class="post-body">{@html data.html}</div>
  </article>
</main>

<style>
  .post {
    padding: 72px 0 96px;
  }

  header {
    padding-bottom: 28px;
    margin-bottom: 36px;
    border-bottom: 2px solid var(--ink);
  }

  header h1 {
    max-width: 18ch;
    font-size: clamp(2.75rem, 7vw, 4.75rem);
  }

  time {
    display: block;
    margin-top: 18px;
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--ink-2);
  }

  .post-body :global(h1),
  .post-body :global(h2),
  .post-body :global(h3),
  .post-body :global(h4) {
    font-family: var(--serif);
    font-weight: 400;
    line-height: 1.1;
    color: var(--ink);
    scroll-margin-top: 84px;
  }

  .post-body :global(h1) {
    font-size: 2.2rem;
    margin: 36px 0 18px;
  }

  .post-body :global(h2) {
    font-size: 1.65rem;
    margin: 34px 0 14px;
    padding-bottom: 8px;
    border-bottom: 2px solid var(--ink);
  }

  .post-body :global(h3) {
    font-size: 1.3rem;
    margin: 28px 0 10px;
  }

  .post-body :global(h4) {
    font-family: var(--mono);
    font-size: 1rem;
    font-weight: 600;
    margin: 24px 0 8px;
  }

  .post-body :global(p),
  .post-body :global(li) {
    color: var(--ink-2);
    font-size: 1.05rem;
    line-height: 1.7;
  }

  .post-body :global(p) {
    margin-bottom: 18px;
  }

  .post-body :global(ul),
  .post-body :global(ol) {
    margin: 0 0 18px;
    padding-left: 26px;
  }

  .post-body :global(ul) {
    list-style: square;
  }

  .post-body :global(ol) {
    list-style: decimal;
  }

  .post-body :global(li) {
    margin: 5px 0;
  }

  .post-body :global(a) {
    color: var(--ink);
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 2px;
  }

  .post-body :global(strong) {
    color: var(--ink);
    font-weight: 700;
  }

  .post-body :global(blockquote) {
    margin-bottom: 18px;
    padding: 4px 16px;
    border-left: 4px solid var(--accent);
    background: var(--bg-elev);
  }

  .post-body :global(hr) {
    margin: 30px 0;
    border: 0;
    border-top: 2px dashed var(--rule-2);
  }

  .post-body :global(code) {
    padding: 1px 5px;
    border: 1px solid var(--rule-2);
    border-radius: 3px;
    background: var(--bg-elev);
    font-family: var(--mono);
    font-size: 0.85em;
  }

  .post-body :global(pre.doc-code) {
    margin-bottom: 20px;
    padding: 18px;
    overflow-x: auto;
    border: 2px solid var(--ink);
    border-radius: var(--radius);
    background: var(--ink);
    color: var(--cream);
    font-family: var(--mono);
    font-size: 0.85rem;
    line-height: 1.5;
  }

  .post-body :global(pre.doc-code code) {
    padding: 0;
    border: 0;
    background: none;
    color: inherit;
    font-size: inherit;
  }

  .post-body :global(img) {
    margin: 24px 0;
    border: 2px solid var(--ink);
  }

  @media (max-width: 600px) {
    .post {
      padding-top: 48px;
    }
  }
</style>
