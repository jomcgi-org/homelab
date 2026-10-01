<script>
  // Markdown written by an agent or an issue author, rendered from the block
  // vocabulary in markdown.js. Pass `text` for a document or `blocks` for a
  // nested list item or quote; the component recurses on itself for those.
  import Inline from "./Inline.svelte";
  import Markdown from "./Markdown.svelte";
  import { markdownBlocks } from "./markdown.js";

  let { text = null, blocks = null } = $props();

  const items = $derived(blocks ?? markdownBlocks(text));
</script>

{#each items as block, index (index)}
  {#if block.type === "heading"}
    <p class="md-h" class:deep={block.depth > 2}>
      <Inline runs={block.inline} />
    </p>
  {:else if block.type === "paragraph"}
    <p><Inline runs={block.inline} /></p>
  {:else if block.type === "text"}
    <span class="md-t"><Inline runs={block.inline} /></span>
  {:else if block.type === "code"}
    <pre class="md-code" data-lang={block.lang || undefined}><code
        >{block.text}</code
      ></pre>
  {:else if block.type === "quote"}
    <blockquote><Markdown blocks={block.blocks} /></blockquote>
  {:else if block.type === "rule"}
    <hr />
  {:else if block.type === "list"}
    {#if block.ordered}
      <ol start={block.start}>
        {#each block.items as item, itemIndex (itemIndex)}
          <li><Markdown blocks={item.blocks} /></li>
        {/each}
      </ol>
    {:else}
      <ul>
        {#each block.items as item, itemIndex (itemIndex)}
          <li class:task={item.task} class:done={item.task && item.checked}>
            <Markdown blocks={item.blocks} />
          </li>
        {/each}
      </ul>
    {/if}
  {:else if block.type === "table"}
    <div class="md-table">
      <table>
        <thead>
          <tr>
            {#each block.header as cell, cellIndex (cellIndex)}
              <th style:text-align={block.align[cellIndex] ?? undefined}
                ><Inline runs={cell} /></th
              >
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each block.rows as row, rowIndex (rowIndex)}
            <tr>
              {#each row as cell, cellIndex (cellIndex)}
                <td style:text-align={block.align[cellIndex] ?? undefined}
                  ><Inline runs={cell} /></td
                >
              {/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
{/each}
