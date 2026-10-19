<script>
  import Route from "../test/SheetRoute.svelte";
  import { leaderboard } from "../test/sheet-fixtures.js";
  import { page, pageStore } from "../test/profile-page.svelte.js";

  const query = new URLSearchParams(location.search);
  const surface =
    query.get("surface") === "artifact" ? "artifact" : "leaderboard";
  const path =
    surface === "artifact" ? "/artifact/fixture" : "/app/llm-leaderboard";
  page.url = new URL(query.get("route") ?? path, location.origin);
  page.url.search = location.search;
  page.params = { id: "fixture" };
  page.data = {
    leaderboard: query.has("empty") ? {} : leaderboard,
    hasBlog: true,
  };
  pageStore.set(page);
</script>

{#if surface !== "artifact"}
  <div class="fixture-note">
    Synthetic repository preview, invented results. No live acceptance.
  </div>
{/if}
<Route {surface} data={page.data} />

<style>
  .fixture-note {
    padding: 0.4rem 1rem;
    font: 12px monospace;
    background: var(--ds-surface);
    color: var(--ds-ink);
  }
</style>
