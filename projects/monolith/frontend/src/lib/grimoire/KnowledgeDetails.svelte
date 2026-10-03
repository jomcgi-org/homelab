<script>
  import { knowledgeFields } from "./knowledge-fields.js";
  import Markdown from "$lib/public/factory/Markdown.svelte";
  let { entity } = $props();
</script>

{#if entity?.recognition_only}<p>You recognize this name.</p>{/if}
{#each knowledgeFields(entity) as [key, value]}
  <section>
    <h3>{key.replaceAll("_", " ")}</h3>
    {#if typeof value === "string"}<Markdown text={value} />
    {:else if Array.isArray(value)}<ul>
        {#each value as item}<li>
            {typeof item === "object" ? JSON.stringify(item) : item}
          </li>{/each}
      </ul>
    {:else if typeof value === "object"}<dl>
        {#each Object.entries(value) as [label, item]}<dt>
            {label.replaceAll("_", " ")}
          </dt>
          <dd>
            {typeof item === "object" ? JSON.stringify(item) : item}
          </dd>{/each}
      </dl>
    {:else}<p>{value}</p>{/if}
  </section>
{/each}

<style>
  section {
    margin-top: 18px;
    overflow-wrap: anywhere;
  }
  h3 {
    text-transform: capitalize;
  }
  dl {
    display: grid;
    grid-template-columns: 1fr 2fr;
    gap: 8px;
  }
  dd {
    margin: 0;
  }
</style>
