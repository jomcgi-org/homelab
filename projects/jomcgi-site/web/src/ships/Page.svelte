<script>
  import { onMount } from "svelte";
  import ShipsMap from "./ShipsMap.svelte";
  import { product } from "./sink.js";
  import "./sheet.css";
  let snapshot = $state(null);
  let heatKey = $state(null);
  let message = $state("Loading vessel snapshot…");
  onMount(() => {
    const request = new AbortController();
    let timer;
    let previous;
    async function read(key) {
      const res = await product(key, request.signal);
      if (!res.ok) throw new Error("Ships product unavailable");
      return res.json();
    }
    async function poll() {
      try {
        const latest = await read("latest.json");
        if (previous !== latest.snapshot) {
          snapshot = await read(latest.snapshot);
          previous = latest.snapshot;
        }
        heatKey = latest.heat;
        message = "";
      } catch {
        message = "Snapshot update unavailable";
      } finally {
        if (!request.signal.aborted) timer = setTimeout(poll, 120000);
      }
    }
    poll();
    return () => {
      request.abort();
      clearTimeout(timer);
    };
  });
</script>

<div class="ships-sheet">
  <header>
    {message}
    {#if snapshot}Snapshot <time datetime={snapshot.generated_at}
        >{snapshot.generated_at}</time
      >{/if}
  </header>
  <section aria-label="Ships map">
    {#if snapshot}<ShipsMap vessels={snapshot.vessels} {heatKey} />{/if}
  </section>
</div>

<style>
  :global(body) {
    margin: 0;
  }
  section {
    position: relative;
  }
  header {
    padding: 0.6rem;
  }
</style>
