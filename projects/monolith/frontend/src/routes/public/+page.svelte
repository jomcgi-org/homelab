<script>
  import { onMount, untrack } from "svelte";
  import { Seo } from "$lib/public/components";
  import ProfileSheet from "$lib/public/components/ProfileSheet.svelte";
  import { LOCATION } from "$lib/public/seo.js";
  import HomepageRack from "./HomepageRack.svelte";
  import { buildMarquee, startHomepageStatsPolling } from "./homepage-stats.js";

  let { data } = $props();
  let stats = $state(untrack(() => data.stats));
  let statsItems = $state(untrack(() => buildMarquee(data.stats)));

  // Keep the existing five-minute fetch and independent 30-second age tick.
  // The poller retains the last usable snapshot and aborts on navigation away.
  onMount(() =>
    startHomepageStatsPolling(data.stats, (snapshot) => {
      stats = snapshot;
      statsItems = buildMarquee(snapshot);
    }),
  );
</script>

<Seo
  title="Joe McGinley · Senior Platform Engineer"
  description="Joe McGinley's homelab: a four-node Kubernetes cluster running agents, maps, and a knowledge graph."
  path="/"
/>

<ProfileSheet
  sections={[
    { id: "about", label: "About" },
    { id: "homelab", label: "Homelab" },
  ]}
>
  <section class="edition" id="about" aria-labelledby="about-heading">
    <div class="ed-head">
      Joe McGinley <span class="end">BUILT THIS SITE TOO</span>
    </div>
    <div class="ed-lead">
      <h1 id="about-heading">
        ten years ago i was underwriting policies and winning insurance
        hackathons. now i'm building production grade infra for weekend side
        quests and keeping <a href="https://semgrep.dev">semgrep</a> online.
      </h1>
      <p>
        I'm Joe, in {LOCATION.short}.<br />Monorepo enthusiast. Care<em>mad</em> about
        developer experience.
      </p>
    </div>
    <div class="ed-foot hero-cta-row">
      <a href="/slop/factory" class="factory-cta" aria-label="Slop Factory"
        ><s aria-hidden="true">Software</s> Slop Factory
        <span aria-hidden="true">→</span></a
      >
      <a href="#homelab">SEE MY HOMELAB</a>
    </div>
  </section>

  <section class="edition" aria-labelledby="stats-heading">
    <div class="ed-head">
      <h2 id="stats-heading">~/homelab</h2>
      <span class="end">Cached snapshot</span>
    </div>
    {#if statsItems.length > 1}
      <ul class="divided" aria-label="Homelab stats">
        {#each statsItems.slice(1) as item}<li>{item}</li>{/each}
      </ul>
    {:else}
      <p class="stats-empty" role="status">Stats unavailable.</p>
    {/if}
  </section>

  <HomepageRack {stats} />
</ProfileSheet>

<style>
  .ed-head h2 {
    margin: 0;
    font: inherit;
    color: var(--ink);
  }
  .stats-empty {
    padding: 0.8rem 1rem;
    margin: 0;
    font-size: 0.88rem;
  }
  /* The primary action is a control, so its accent carries intent. */
  .hero-cta-row .factory-cta {
    color: var(--accent-ink);
    font-weight: 700;
    border-bottom: 2px solid var(--accent-ink);
  }
  .hero-cta-row a {
    padding: 0.9em 1rem;
  }
</style>
