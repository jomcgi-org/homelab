<script>
  import Route from "./Route.svelte";
  import { setPage } from "./page.svelte.js";
  import { installNetwork, installChallenge, routeData } from "./fixtures.js";
  let surface = $state("home");
  let data = $state({});
  let route = $state("");
  function navigate() {
    const url = new URL(location.href);
    surface =
      url.pathname === "/engineering"
        ? "engineering"
        : url.pathname.includes("/chat/s/")
          ? "share"
          : url.pathname.split("/")[3] || "home";
    const state = url.searchParams.get("state") ?? "normal";
    data = routeData(state);
    window.fixtureRequests = installNetwork(state);
    installChallenge();
    setPage(url.href, surface, data);
    route = url.pathname;
  }
  navigate();
  function click(event) {
    const anchor = event.target.closest("a");
    if (
      !anchor ||
      event.defaultPrevented ||
      event.button ||
      event.metaKey ||
      event.ctrlKey ||
      anchor.target
    )
      return;
    const next = new URL(anchor.href);
    if (
      next.origin !== location.origin ||
      next.hash ||
      (!next.pathname.startsWith("/app/grimoire") &&
        next.pathname !== "/engineering")
    )
      return;
    event.preventDefault();
    history.pushState({}, "", next);
    navigate();
  }
</script>

<svelte:window onclick={click} onpopstate={navigate} />
{#key route}<Route {surface} {data} />{/key}
