<script>
  import { LINK_LABELS } from "$lib/public/homepage-stack.js";

  /**
   * @type {{
   *   project: object,
   *   expanded?: boolean,
   *   onselect: () => void,
   * }}
   */
  let { project, expanded = false, onselect } = $props();

  const links = $derived(
    LINK_LABELS.filter(([key]) => project.links[key]).map(([key, label]) => ({
      key,
      label,
      href: project.links[key],
      external: project.links[key].startsWith("http"),
    })),
  );
</script>

<article class="card" class:expanded>
  <button
    class="card-face"
    type="button"
    onclick={onselect}
    aria-expanded={expanded}
  >
    <h3>{project.name}</h3>
    <p class="blurb">{project.blurb}</p>
    <ul class="tags">
      {#each project.tags as tag}
        <li>{tag}</li>
      {/each}
    </ul>
  </button>
  {#if expanded}
    <div class="story">
      <p>{project.engineering}</p>
      <div class="actions">
        {#each links as link (link.key)}
          <a
            class="card-btn"
            class:card-btn-live={link.key === "live"}
            href={link.href}
            target={link.external ? "_blank" : undefined}
            rel={link.external ? "noopener" : undefined}
          >
            {link.label} &nearr;
          </a>
        {/each}
      </div>
    </div>
  {/if}
</article>

<style>
  .card {
    background: var(--paper);
    border: 2px solid var(--ink);
    transition:
      transform 0.12s ease,
      box-shadow 0.12s ease;
  }
  .card:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard-sm);
  }
  .card.expanded {
    background: var(--accent);
    box-shadow: var(--shadow-hard);
  }
  .card-face {
    display: block;
    width: 100%;
    padding: 18px 20px 14px;
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    font: inherit;
    color: var(--ink);
  }
  h3 {
    font-family: var(--mono);
    font-size: 17px;
    font-weight: 800;
    letter-spacing: 0.04em;
    margin: 0 0 8px;
  }
  .blurb {
    font-size: 14px;
    line-height: 1.45;
    margin: 0 0 12px;
    color: var(--ink-2);
  }
  .card.expanded .blurb {
    color: var(--ink);
  }
  .tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .tags li {
    font-family: var(--mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    padding: 2px 6px;
    border: 1px solid var(--rule);
  }
  .story {
    padding: 0 20px 18px;
  }
  .story p {
    font-size: 14px;
    line-height: 1.5;
    margin: 0 0 14px;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .card-btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    text-decoration: none;
    color: var(--ink);
    background: var(--paper);
    border: 2px solid var(--ink);
    padding: 6px 10px;
  }
  .card-btn:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard-sm);
  }
  .card-btn-live {
    background: var(--ink);
    color: var(--paper);
  }
</style>
