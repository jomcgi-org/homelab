<script>
  import Page from "../src/routes/public/app/dr-jobs/+page.svelte";
  import { setPage } from "./state.svelte.js";
  import "../src/lib/global.css";
  import "../src/lib/public/styles/design-system.css";
  let { data, url } = $props();
  // Initialize both page APIs before the production child reads its URL.
  // A preview navigation starts a new document, so these props are initial.
  // svelte-ignore state_referenced_locally
  setPage(url, data);
</script>

<div class="public-theme" hidden></div>
<p class="td fixture-scope">
  Synthetic fixture preview · DR jobs · listings are invented
</p>
<Page {data} />

<style>
  .fixture-scope {
    margin: 0;
    padding: 0.4rem 1rem;
    background: var(--band);
    color: var(--ink-2);
    font: 0.75rem var(--font-code);
  }
</style>
