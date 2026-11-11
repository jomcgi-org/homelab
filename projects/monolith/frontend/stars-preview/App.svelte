<script>
  import Route from "../test/stars/Route.svelte";
  import { setPage } from "../test/profile-page.svelte.js";
  import { data } from "../test/stars/fixtures.js";
  setPage(`https://jomcgi.dev/app/stars${location.search}`, data);
</script>

<p class="fixture-note">
  Synthetic stars fixture: invented forecast and history. Real MapLibre and
  OpenFreeMap geography. No live acceptance.
</p>
<Route {data} />

<style>
  .fixture-note {
    margin: 0;
    padding: 0.4rem 1rem;
    font: 12px monospace;
    background: var(--ds-surface);
    color: var(--ds-ink);
  }
</style>
