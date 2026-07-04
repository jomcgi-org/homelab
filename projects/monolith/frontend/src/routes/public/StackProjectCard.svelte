<script>
  /**
   * @type {{
   *   project: object,
   *   expanded?: boolean,
   *   onselect: () => void,
   * }}
   */
  let { project, expanded = false, onselect } = $props();
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
        {#if project.links.live}
          <a class="btn btn-live" href={project.links.live}>Visit live &nearr;</a>
        {/if}
        <a class="btn" href={project.links.readme} target="_blank" rel="noopener">
          Read the code &nearr;
        </a>
      </div>
    </div>
  {/if}
</article>

<style>
  .card {
    background: var(--paper);
    border: 2px solid var(--ink);
    box-shadow: var(--shadow-hard-sm);
    transition:
      transform 0.12s ease,
      box-shadow 0.12s ease;
  }
  .card:hover {
    transform: translate(-2px, -2px);
    box-shadow: var(--shadow-hard);
  }
  .card.expanded {
    background: var(--accent);
    box-shadow: var(--shadow-hard);
  }
  .card-face {
    display: block;
    width: 100%;
    padding: 14px 16px;
    text-align: left;
    background: none;
    border: none;
    cursor: pointer;
    font: inherit;
    color: var(--ink);
  }
  h3 {
    font-family: var(--mono);
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.08em;
    margin: 0 0 6px;
  }
  .blurb {
    font-size: 13px;
    line-height: 1.4;
    margin: 0 0 10px;
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
    padding: 0 16px 14px;
  }
  .story p {
    font-size: 13px;
    line-height: 1.5;
    margin: 0 0 12px;
  }
  .actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }
  .btn {
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
    box-shadow: var(--shadow-hard-sm);
  }
  .btn:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard);
  }
  .btn-live {
    background: var(--ink);
    color: var(--paper);
  }
</style>
