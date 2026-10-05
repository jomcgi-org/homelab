<script>
  import Post from "../src/routes/public/blog/[slug]/+page.svelte";
  import BlogLayout from "../src/routes/public/blog/+layout.svelte";
  import "../src/lib/public/styles/design-system.css";
  import data from "./fixtures/blog-page.json";
  import { page } from "./state.svelte.js";
  page.params.slug = data.slug;
  page.data = data;
</script>

<p id="preview-scope" tabindex="-1">
  Synthetic fixture preview · 4090 blog only · timings are invented
</p>
<div class="public-theme" hidden></div>
<BlogLayout><Post {data} /></BlogLayout>
<span id="synthetic-source" hidden>Invented fixture report</span>

<style>
  #preview-scope {
    margin: 0;
    padding: 0.4rem 1rem;
    font:
      12px ui-monospace,
      monospace;
    background: #ffde01;
    color: #1a1a1a;
  }
</style>
