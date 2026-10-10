<script>
  // The renderer HTML-escapes every character of the source and emits only its
  // own fixed tags, so a handout cannot inject script or attributes.
  import { renderMarkdown } from "$lib/components/notes/markdown.js";

  let { markdown = "" } = $props();
  const html = $derived(renderMarkdown(String(markdown ?? ""), new Map()));
</script>

<div class="handout-text">{@html html}</div>

<style>
  .handout-text {
    overflow-wrap: anywhere;
  }
  .handout-text :global(p),
  .handout-text :global(ul),
  .handout-text :global(ol),
  .handout-text :global(blockquote) {
    margin: 0.5rem 0;
  }
  .handout-text :global(blockquote) {
    padding-left: 0.75rem;
    border-left: 3px solid var(--grim-line);
    color: var(--grim-text-dim);
  }
  .handout-text :global(pre) {
    white-space: pre-wrap;
    background: var(--grim-surface-2);
    padding: 0.5rem;
  }
  .handout-text :global(table) {
    border-collapse: collapse;
  }
  .handout-text :global(th),
  .handout-text :global(td) {
    border: 1px solid var(--grim-line);
    padding: 0.25rem 0.5rem;
  }
</style>
