<script>
  import Route from "../test/MovingRoute.svelte";
  import { page } from "./stores.js";
  import pages from "virtual:moving-pages";
  let data = $state();
  function navigate() {
    const url = new URL(location.href);
    const mode =
      url.searchParams.get("mode") === "manage" ? "manage" : "dashboard";
    const variant = url.searchParams.get("fixture") ?? "populated";
    data = structuredClone(pages[variant][mode]);
    if (mode !== "manage") data.scope = url.searchParams.get("scope") ?? "mine";
    // A synthetic friends host exercises the real root Nav suppression.
    page.set({
      url: new URL(
        `${url.pathname}${url.search}`,
        "https://friends.example.test",
      ),
      data,
      status: 200,
      error: null,
    });
  }
  navigate();
</script>

<svelte:window onpopstate={navigate} />
<Route {data} />
