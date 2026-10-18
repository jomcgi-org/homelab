<script>
  // The site's sections, for the spine of a page that renders no site nav
  // bar: the technical-drawing pages carry the trail and the scheme switch
  // as their only chrome, and the trail's root is the way home. This keeps
  // the other sections one click away on a wide screen; on a phone the
  // spine folds into the chrome row and this list hides with it.
  import { page } from "$app/state";
  import { apps } from "$lib/public/apps.js";

  /** @type {{ here?: string }} */
  let { here = "" } = $props();

  // The blog section only exists once a post is published, the same rule
  // the retired nav bar applied.
  const sections = $derived([
    { slug: "engineering", label: "Engineering", href: "/engineering" },
    ...(page.data?.hasBlog
      ? [{ slug: "blog", label: "Blog", href: "/blog" }]
      : []),
    { slug: "docs", label: "Docs", href: "/docs" },
    { slug: "cv", label: "CV", href: "/cv" },
  ]);
</script>

<nav class="index site-index" aria-label="Site">
  <p class="sec-label">/ Site</p>
  <div class="index-list">
    {#each sections as section (section.slug)}
      <a
        href={section.href}
        class:active={here === section.slug}
        aria-current={here === section.slug ? "page" : undefined}
        >{section.label}</a
      >
    {/each}
  </div>
  <details class="index-group">
    <summary>
      <span class="group-name">Apps</span>
      <span class="group-count">{apps.length}</span>
    </summary>
    <div class="index-list">
      {#each apps as app (app.slug)}
        <a href={app.href} class:active={here === app.slug}>
          <span>{app.label}</span>
          <small>{app.desc}</small>
        </a>
      {/each}
    </div>
  </details>
</nav>

<style>
  .index-list a {
    flex-wrap: wrap;
  }

  small {
    flex-basis: 100%;
    color: var(--ink-2);
    font-size: 0.85em;
    line-height: 1.3;
  }
</style>
