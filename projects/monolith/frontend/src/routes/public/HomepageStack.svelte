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

<section class="stack" aria-label="What this homelab runs">
  <p class="eyebrow">The stack, top to bottom. Click a project.</p>
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
                onselect={() => setSelected(selected === project.id ? null : project.id)}
              />
            {/each}
          </div>
        {:else}
          <ul class="strip">
            {#each layer.items as item}
              <li>
                {#if item.href}
                  <a href={item.href} target="_blank" rel="noopener">{item.name}</a>
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
    box-shadow: var(--shadow-hard);
    background: var(--paper);
  }
  .layer {
    position: relative;
    padding: 28px 24px 20px;
    border-top: 2px dashed var(--rule-2);
  }
  .layer:first-child {
    border-top: none;
  }
  .layer.alt {
    background: var(--cream);
  }
  .layer-label {
    position: absolute;
    top: -1px;
    left: 16px;
    transform: translateY(-50%);
    font-family: var(--mono);
    font-size: 8px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    background: var(--paper);
    border: 1px dashed var(--rule-2);
    padding: 2px 8px;
  }
  .layer:first-child .layer-label {
    top: 0;
    transform: none;
    border: none;
    padding-left: 0;
    position: static;
    display: block;
    margin-bottom: 12px;
  }
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
    gap: 16px;
    align-items: start;
  }
  .strip {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 12px;
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
    color: var(--ink-2);
    text-decoration: none;
    border: 1px solid var(--rule);
    background: var(--paper);
    padding: 4px 10px;
    display: inline-block;
  }
  .strip li a:hover {
    color: var(--ink);
    border-color: var(--ink);
  }
</style>
