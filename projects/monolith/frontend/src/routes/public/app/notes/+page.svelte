<script>
  import { browser } from "$app/environment";
  import NotesApp from "./NotesApp.svelte";

  let { data } = $props();
</script>

<svelte:head>
  <title>Notes · jomcgi.dev</title>
  <meta
    name="description"
    content="A neo-brutalist chat box wired to my public knowledge graph. Ask a question and switch to the graph to deep-dive into the notes the conversation touched."
  />
</svelte:head>

<h1 class="sr-only">Chat with my knowledge graph</h1>
<p class="sr-only">
  Ask questions of my public notes; every answer cites the notes it came from.
</p>

{#if browser}
  <NotesApp {data} />
{/if}

<style>
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0 0 0 0);
    white-space: nowrap;
    border: 0;
  }
</style>
