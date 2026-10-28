<script>
  import Route from "../test/TripsRoute.svelte";
  import { tripsData } from "../test/trips-fixtures.js";
  import { setPage } from "../test/profile-page.svelte.js";
  let surface = $state("index");
  let data = $state(tripsData());
  function navigate() {
    const url = new URL(location.href);
    const path = url.pathname;
    surface = path.endsWith("/timeline")
      ? "timeline"
      : path.includes("/day/")
        ? "day"
        : path.endsWith("fixture-walk")
          ? "trip"
          : "index";
    data = tripsData();
    if (surface === "day") data.dayNumber = Number(path.split("/").at(-1));
    if (url.searchParams.has("empty")) {
      data.points = [];
      data.index.trips = [];
    }
    setPage(url.href, data);
  }
  navigate();
  function click(event) {
    const anchor = event.target.closest("a");
    if (
      !anchor ||
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey ||
      anchor.hasAttribute("download")
    )
      return;
    event.preventDefault();
    const url = new URL(anchor.href);
    if (
      url.origin !== location.origin ||
      !url.pathname.startsWith("/app/trips")
    )
      return;
    history.pushState({}, "", url);
    navigate();
    window.scrollTo(0, 0);
  }
</script>

<svelte:window onclick={click} onpopstate={navigate} />
<div class="fixture-note">
  Synthetic trips preview. Invented route and image swatches. No live
  acceptance.
</div>
<Route {surface} {data} />

<style>
  .fixture-note {
    padding: 0.4rem 1rem;
    font: 12px monospace;
    background: var(--ds-surface);
    color: var(--ds-ink);
  }
</style>
