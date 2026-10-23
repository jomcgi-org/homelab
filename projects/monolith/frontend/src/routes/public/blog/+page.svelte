<script>
  import { Seo } from "$lib/public/components";
  import { formatDate } from "./blog.js";
  import Trail from "./Trail.svelte";

  let { data } = $props();
  let activeSlug = $state("");

  $effect(() => {
    const slugs = data.posts.map((post) => post.slug);
    if (!slugs.includes(activeSlug)) activeSlug = slugs[0] ?? "";
  });

  $effect(() => {
    const posts = data.posts;
    if (!posts.length) return;
    const entries = document.querySelectorAll("[data-post-slug]");
    const observer = new IntersectionObserver(
      (observed) => {
        const visible = observed
          .filter((item) => item.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) activeSlug = visible[0].target.dataset.postSlug;
      },
      { rootMargin: "-15% 0px -65% 0px" },
    );
    entries.forEach((entry) => observer.observe(entry));
    return () => observer.disconnect();
  });
</script>

<Seo
  title="Blog · jomcgi.dev"
  description="Technical posts by Joe McGinley."
  path="/blog"
/>

<main class="td sheet blog-page">
  <div class="frame">
    <h1 class="sr-only">Blog</h1>

    <div class="journal">
      <aside class="spine">
        <div class="trail-dock">
          <Trail />
        </div>
        {#if data.months.length}
          <nav class="date-rail" aria-label="Blog index">
            <p class="sec-label">/ Index</p>
            {#each data.months as month, index}
              <details class="rail-month" open={index === 0}>
                <summary>
                  <span class="month-name">{month.label}</span>
                  <span class="group-count">{month.posts.length}</span>
                </summary>
                <div class="rail-days">
                  {#each month.posts as post}
                    <a
                      class:active={activeSlug === post.slug}
                      aria-current={activeSlug === post.slug
                        ? "location"
                        : undefined}
                      href={`/blog/${post.slug}`}
                    >
                      <span>{post.date.slice(8)}</span>
                      <small>{post.title}</small>
                    </a>
                  {/each}
                </div>
              </details>
            {/each}
          </nav>
        {/if}
      </aside>

      <div class="content">
        {#if !data.posts.length}
          <section class="empty-entry">
            <p class="sec-label">/ Status</p>
            <h2>Nothing published yet.</h2>
          </section>
        {:else}
          <section class="editions ruled" aria-label="Blog posts">
            {#each data.posts as post}
              <article class="edition" data-post-slug={post.slug}>
                <p class="ed-head">
                  <time datetime={post.date}>{formatDate(post.date)}</time>
                </p>

                <div class="ed-lead">
                  <h2><a href={`/blog/${post.slug}`}>{post.title}</a></h2>
                  <p>{post.summary}</p>
                </div>
              </article>
            {/each}
          </section>
        {/if}
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

  /* The index keeps its smaller headlines and continuous ruled editions.
     Its date rail is a grouped journal index, not the post's TOC strip. */
  .blog-page .ed-head {
    display: block;
    gap: normal;
    color: var(--ink);
  }

  .blog-page .rail-month summary::-webkit-details-marker {
    display: none;
  }

  .blog-page .empty-entry a {
    color: var(--ink-2);
    text-decoration: underline;
    text-underline-offset: 0.2em;
  }

  .blog-page .empty-entry a:hover {
    color: var(--accent-ink);
  }

  .blog-page .date-rail {
    margin-top: 0;
  }

  .blog-page .rail-month summary {
    display: flex;
    gap: 0.6em;
    align-items: baseline;
    justify-content: space-between;
    padding: 0.5em 0;
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    list-style: none;
  }

  .blog-page .rail-month[open] .group-count::after {
    content: "\2212";
  }

  .blog-page .month-name {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68em;
    font-weight: 500;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .blog-page .rail-month summary:hover .month-name {
    color: var(--accent-ink);
  }

  .blog-page .rail-days {
    padding: 0.35em 0;
    border-bottom: 1px solid var(--line);
  }

  .blog-page .rail-month a {
    display: grid;
    grid-template-columns: 2em minmax(0, 1fr);
    gap: 0.65em;
    align-items: start;
    padding: 0.42em 0 0.42em 0.6rem;
    border-left: 2px solid transparent;
    color: var(--ink-2);
    text-decoration: none;
  }

  .blog-page .rail-month a > span {
    font-family: var(--font-code);
    font-size: 0.75em;
  }

  .blog-page .rail-month small {
    display: -webkit-box;
    overflow: hidden;
    font-size: 0.7em;
    line-height: 1.3;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
  }

  .blog-page .rail-month a:hover,
  .blog-page .rail-month a.active {
    color: var(--accent-ink);
  }

  .blog-page .rail-month a.active {
    border-left-color: var(--accent-ink);
  }

  .blog-page .ed-head time {
    color: var(--ink-2);
  }

  .blog-page .ed-lead h2 {
    max-width: 40ch;
    margin: 0;
    font-size: clamp(1.1rem, 1.6vw, 1.35rem);
    font-weight: 700;
    letter-spacing: -0.02em;
    line-height: 1.15;
  }

  .blog-page .ed-lead p {
    margin: 0.5rem 0 0;
  }

  .blog-page .empty-entry {
    padding: 1rem;
    border: 1px solid var(--ink);
  }

  .blog-page .empty-entry h2 {
    margin: 1rem 0 0;
    font-size: 1.2rem;
    font-weight: 700;
    letter-spacing: -0.02em;
  }

  .blog-page .empty-entry > p:last-child {
    margin: 0.65rem 0 0;
    color: var(--ink-2);
    font-size: 0.92rem;
    line-height: 1.55;
  }

  @media (max-width: 760px) {
    .blog-page .trail-dock {
      display: none;
    }

    .blog-page .date-rail {
      position: sticky;
      z-index: 4;
      top: 0;
      display: flex;
      gap: 1.5em;
      align-items: baseline;
      /* Match the sheet's mobile gutter. The old 1.35em overreached the
         viewport by 0.35em on each side, creating page-level scrolling. */
      margin: 1.2rem -1em 2rem;
      padding: 0.5em 1em;
      overflow-x: auto;
      border-top: 1px solid var(--line);
      border-bottom: 1px solid var(--stroke);
      background: var(--sheet);
    }

    .blog-page .date-rail > .sec-label {
      display: none;
    }

    .blog-page .rail-month {
      display: flex;
      gap: 0.65em;
      align-items: center;
      margin: 0;
    }

    .blog-page .rail-month summary {
      min-width: max-content;
      padding: 0.65em 0;
      border-bottom: 2px solid transparent;
    }

    .blog-page .rail-days {
      display: flex;
      gap: 0.35em;
      align-items: center;
      padding: 0;
      border-bottom: 0;
    }

    .blog-page .rail-month a {
      display: block;
      min-width: 2.2em;
      padding: 0.65em 0.4em;
      border-left: 0;
      border-bottom: 2px solid transparent;
      text-align: center;
    }

    .blog-page .rail-month a:hover,
    .blog-page .rail-month a.active {
      border-bottom-color: var(--accent-ink);
    }

    .blog-page .rail-month small {
      display: none;
    }
  }
</style>
