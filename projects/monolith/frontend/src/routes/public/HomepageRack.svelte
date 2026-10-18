<script>
  import { sandboxRestoreMs } from "$lib/public/fcstory/metrics.js";
  import { apps } from "$lib/public/apps.js";

  const liveMaps = ["ships", "stars", "hikes", "trips"].map((slug) =>
    apps.find((app) => app.slug === slug),
  );
  const featuredApps = apps.filter((app) => app.featured);
  let { stats } = $props();

  function buildEyebrow(statsData) {
    if (!statsData?.cluster) return "HOMELAB";
    const c = statsData.cluster;
    const parts = [];
    if (c.nodes != null) parts.push(`${c.nodes} nodes`);
    if (c.cpu_capacity_cores != null)
      parts.push(`${c.cpu_capacity_cores} CPUs`);
    if (c.memory_capacity_gb != null) parts.push(`${c.memory_capacity_gb} GB`);
    return parts.length > 0 ? parts.join(" · ") : "HOMELAB";
  }
  const eyebrow = $derived(buildEyebrow(stats));
</script>

<section class="edition" id="homelab" aria-label="Homelab hardware and systems">
  <div class="ed-head">{eyebrow}</div>
  <div class="ed-lead"><h2>HOMELAB</h2></div>
  <dl class="spec" aria-label="Hardware">
    <div class="row">
      <dt>NODE-1</dt>
      <dd>12 cpu · 16 gb · postgres primary</dd>
    </div>
    <div class="row">
      <dt>NODE-2</dt>
      <dd>12 cpu · 16 gb · serves this page</dd>
    </div>
    <div class="row">
      <dt>NODE-3</dt>
      <dd>12 cpu · 16 gb · storage replicas</dd>
    </div>
  </dl>
  <div class="ed-head"><h3>Live maps</h3></div>
  <div class="ed-foot maps">
    {#each liveMaps as app (app.slug)}<a href={app.href}>{app.label}</a>{/each}
  </div>
  {#each featuredApps as app (app.slug)}
    <div class="ed-head">
      <h3>{app.label.toUpperCase()}</h3>
      {#if app.slug === "grimoire"}<span class="end">NODE-2</span>{/if}
    </div>
    <div class="ed-body prose">
      {#if app.slug === "grimoire"}
        <p>
          A D&amp;D campaign manager built on grants: the same monster renders
          full stats for the DM, redacted stats for a player who has fought it,
          and just a name for one who has only heard rumours. <a
            href="/app/grimoire">play &rarr;</a
          >
        </p>
      {:else if app.slug === "firecracker"}
        <p>
          Boot a workload once, freeze it, restore the snapshot for every
          request. The guest never holds a real secret; an egress proxy swaps
          placeholder tokens for credentials at the network hop. Watch a microVM
          restore from disk in <b>{sandboxRestoreMs}ms</b>.
          <a href="/ember/firecracker">watch it restore &rarr;</a>
        </p>
      {/if}
    </div>
  {/each}
  <div class="ed-head"><h3>Under the hood</h3></div>
  <div class="ed-body prose">
    <ul>
      <li>
        Tag <b>@Bosun</b> in a Discord thread and an agent answers;
        <b>EmberVM</b>
        gives every job its own microVM. <a href="/ember">how &rarr;</a>
      </li>
      <li>
        One Postgres backs every app; <a href="/app/notes">notes</a> is a public RAG
        over it.
      </li>
      <li>
        Bazel builds every image, ArgoCD ships the repo. <a
          href="/docs/platform/architecture#4-gitops-and-delivery"
          >the pipeline &rarr;</a
        >
      </li>
    </ul>
  </div>
</section>

<style>
  .ed-head h3 {
    margin: 0;
    font: inherit;
    color: var(--ink);
  }
  .spec + .ed-head,
  .ed-body + .ed-head {
    border-top: 1px solid var(--stroke);
  }
  .maps {
    border-top: 0;
    border-bottom: 1px solid var(--stroke);
  }
  .ed-head + .ed-body {
    border-top: 0;
  }
</style>
