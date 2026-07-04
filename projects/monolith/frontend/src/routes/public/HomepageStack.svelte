<script>
  import { page } from "$app/stores";
  import { goto } from "$app/navigation";
  import { stack } from "$lib/public/homepage-stack.js";
  import StackProjectCard from "./StackProjectCard.svelte";

  // Selection is mirrored to the URL (?project=<id>) so cards are
  // deep-linkable and the browser back button pops selection naturally.
  const selected = $derived($page.url.searchParams.get("project"));

  function setSelected(id) {
    const url = new URL($page.url);
    if (id) url.searchParams.set("project", id);
    else url.searchParams.delete("project");
    goto(url, { keepFocus: true, noScroll: true, replaceState: false });
  }

  function handleKeydown(e) {
    if (e.key === "Escape" && selected) {
      e.preventDefault();
      setSelected(null);
    }
  }
</script>

<svelte:window onkeydown={handleKeydown} />

<section class="stack" id="homelab" aria-label="What this homelab runs">
  <p class="eyebrow">The stack, top to bottom. Click a system.</p>
  <h2>WHAT RUNS HERE</h2>

  <div class="strata">
    {#each stack as layer, i}
      <div class="layer" class:alt={i % 2 === 1} data-kind={layer.kind}>
        <span class="layer-label">{layer.label}</span>
        {#if layer.kind === "projects"}
          <div class="cards">
            {#each layer.items as project (project.id)}
              <StackProjectCard
                {project}
                expanded={selected === project.id}
                onselect={() =>
                  setSelected(selected === project.id ? null : project.id)}
              />
            {/each}
          </div>
        {:else}
          <ul class="strip">
            {#each layer.items as item}
              <li>
                {#if item.href}
                  <a href={item.href}>{item.name}</a>
                {:else}
                  <span>{item.name}</span>
                {/if}
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/each}
  </div>
</section>

<style>
  .stack {
    max-width: 1360px;
    margin: 0 auto;
    padding: 48px 32px;
  }
  h2 {
    font-family: var(--mono);
    font-size: 28px;
    letter-spacing: 0.04em;
    margin: 8px 0 24px;
  }
  .strata {
    border: 2px solid var(--ink);
    background: var(--paper);
  }
  .layer {
    padding: 20px 24px;
    border-top: 2px dashed var(--rule-2);
  }
  .layer:first-child {
    border-top: none;
  }
  .layer.alt {
    background: var(--cream);
  }
  .layer[data-kind="projects"] {
    padding: 20px 24px 28px;
  }
  .layer-label {
    display: block;
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-bottom: 14px;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 16px;
    align-items: start;
  }
  .strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 10px;
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .strip li a,
  .strip li span {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    text-decoration: none;
    padding: 5px 12px;
    display: inline-block;
  }
  .strip li span {
    color: var(--ink-2);
    border: 1px solid var(--rule);
    background: var(--paper);
  }
  .strip li a {
    font-weight: 700;
    color: var(--ink);
    border: 2px solid var(--ink);
    background: var(--paper);
    transition:
      transform 0.12s ease,
      box-shadow 0.12s ease;
  }
  .strip li a:hover {
    transform: translate(-1px, -1px);
    box-shadow: var(--shadow-hard-sm);
    background: var(--accent);
  }
</style>
