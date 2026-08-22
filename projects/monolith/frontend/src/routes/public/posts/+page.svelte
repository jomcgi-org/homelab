<script>
  import { Seo } from "$lib/public/components";

  let { data } = $props();
</script>

<Seo
  title="Posts · jomcgi.dev"
  description="Technical posts by Joe McGinley."
  path="/posts"
/>

<main class="posts page">
  <div class="wrap-narrow">
    <h1 class="display">Posts</h1>

    {#if data.posts.length === 0}
      <p class="empty">Nothing published yet.</p>
    {:else}
      <ol class="post-list">
        {#each data.posts as post}
          <li>
            <article>
              <div class="post-heading">
                <h2>
                  <a href={`/posts/${post.slug}`}>{post.title}</a>
                </h2>
                <time datetime={post.date}>{post.date}</time>
              </div>
              <p>{post.summary}</p>
            </article>
          </li>
        {/each}
      </ol>
    {/if}
  </div>
</main>

<style>
  .posts {
    padding: 72px 0 96px;
  }

  h1 {
    font-size: clamp(3rem, 8vw, 5rem);
    margin-bottom: 40px;
  }

  .empty {
    font-size: 1.05rem;
    color: var(--ink-2);
  }

  .post-list {
    list-style: none;
    border-top: 2px solid var(--ink);
  }

  .post-list li {
    border-bottom: 1px solid var(--rule-2);
  }

  article {
    padding: 24px 4px;
  }

  .post-heading {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 24px;
  }

  h2 {
    font-family: var(--serif);
    font-size: 1.75rem;
    font-weight: 400;
    line-height: 1.1;
  }

  h2 a {
    text-decoration: underline;
    text-decoration-color: var(--coral);
    text-decoration-thickness: 2px;
    text-underline-offset: 4px;
  }

  h2 a:hover {
    text-decoration-thickness: 3px;
  }

  time {
    flex: 0 0 auto;
    font-family: var(--mono);
    font-size: 0.75rem;
    color: var(--ink-2);
  }

  article p {
    max-width: 68ch;
    margin-top: 10px;
    color: var(--ink-2);
    line-height: 1.6;
  }

  @media (max-width: 600px) {
    .posts {
      padding-top: 48px;
    }

    .post-heading {
      display: block;
    }

    time {
      display: block;
      margin-top: 8px;
    }
  }
</style>
