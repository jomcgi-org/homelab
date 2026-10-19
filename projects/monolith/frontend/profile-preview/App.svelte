<script>
  import Route from "../test/ProfileRoute.svelte";
  import { setPage } from "../test/profile-page.svelte.js";
  import { reroute } from "../src/hooks.js";
  import { stats } from "./fixtures.js";

  let surface = $state("home");
  let data = $state({});
  function navigate(url) {
    const routed = reroute({ url }) ?? url.pathname;
    surface =
      {
        "/public/": "home",
        "/public/cv": "cv",
        "/public/engineering": "engineering",
        "/public/ember": "ember",
        "/public/ember/bazel": "bazel-fallback",
        "/public/ember/postgres": "postgres-fallback",
        "/public/legacy-fixture": "legacy",
      }[routed] ?? "error";
    data = {
      stats: url.searchParams.has("empty") ? null : stats,
      hasBlog: true,
      maintenanceBanner: url.searchParams.has("banner")
        ? "Synthetic maintenance notice"
        : "",
    };
    setPage(
      url.href,
      data,
      surface === "error" ? { message: "Fixture route missing" } : null,
    );
  }
  navigate(new URL(location.href));
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
      anchor.target ||
      anchor.hasAttribute("download")
    )
      return;
    const next = new URL(anchor.href);
    if (
      next.origin !== location.origin ||
      next.hash ||
      ![
        "/",
        "/cv",
        "/engineering",
        "/ember",
        "/ember/bazel",
        "/ember/postgres",
        "/legacy-fixture",
      ].includes(next.pathname)
    )
      return;
    event.preventDefault();
    history.pushState({}, "", next);
    navigate(next);
    window.scrollTo(0, 0);
  }
</script>

<svelte:window
  onclick={click}
  onpopstate={() => navigate(new URL(location.href))}
/>
<div class="fixture-note">
  Synthetic repository preview, invented stats. No live acceptance.
  {#if surface.endsWith("-fallback")}
    {surface}: component fixture only. The real route redirects HTTP 308 to
    /ember/firecracker#replay.
  {/if}
</div>
<Route {surface} {data} />
<nav class="fixture-links" aria-label="Fixture routes">
  <a href="/">Home fixture</a><a href="/cv">CV fixture</a><a href="/engineering"
    >Engineering fixture</a
  ><a href="/ember">Ember fixture</a><a href="/ember/bazel"
    >Bazel fallback fixture</a
  ><a href="/ember/postgres">Postgres fallback fixture</a><a
    href="/legacy-fixture">Legacy Nav fixture</a
  >
</nav>

<style>
  .fixture-note,
  .fixture-links {
    padding: 0.4rem 1rem;
    font: 12px monospace;
    background: var(--ds-surface);
    color: var(--ds-ink);
  }
  .fixture-links {
    display: flex;
    flex-wrap: wrap;
    gap: 1rem;
  }
</style>
