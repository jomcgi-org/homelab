<script>
  import { trackingSource } from "./tracking-view.js";
  let { item, detail = false } = $props();
  const source = $derived(trackingSource(item.source_ref));
</script>

<article class="edition tracking-item" aria-label="Tracking-only work">
  <div class="ed-lead">
    <p class="sec-label">{item.tracking_kind ?? "unknown"} · tracking only</p>
    <h3><a href={`/factory/work-items/${item.id}`}>{item.title}</a></h3>
    <dl class="spec">
      <div>
        <dt>Owner</dt>
        <dd>{item.owner || "unknown"}</dd>
      </div>
      <div>
        <dt>Status</dt>
        <dd>{item.state ?? "unknown"}</dd>
      </div>
      <div>
        <dt>Source</dt>
        <dd>
          {#if source}<a href={source} target="_blank" rel="noopener noreferrer"
              >{item.source_ref}</a
            >
          {:else}unknown{/if}
        </dd>
      </div>
      <div>
        <dt>Next action</dt>
        <dd>{item.next_action || "none recorded"}</dd>
      </div>
      <div>
        <dt>Blocker</dt>
        <dd>{item.blocker || "none recorded"}</dd>
      </div>
    </dl>
    <p>Recorded information only. The named owner performs the work.</p>
    {#if item.completion_evidence}
      <p>
        {item.state === "done"
          ? "Recorded external completion"
          : "Previous completion evidence"}: {item.completion_evidence}
      </p>
      <p>Factory accepted delivery has not been recorded by this task.</p>
    {/if}
    {#if detail}
      <p>{item.body ?? ""}</p>
      <dl class="spec">
        <div>
          <dt>Version</dt>
          <dd>{item.tracking_version}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{item.created_at ?? "unknown"}</dd>
        </div>
        <div>
          <dt>Updated</dt>
          <dd>{item.updated_at ?? "unknown"}</dd>
        </div>
        <div>
          <dt>Completed</dt>
          <dd>{item.completed_at ?? "not recorded"}</dd>
        </div>
      </dl>
    {/if}
  </div>
</article>

<style>
  .tracking-item {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .spec div {
    display: grid;
    grid-template-columns: minmax(5em, 1fr) minmax(0, 3fr);
    gap: 0.75em;
  }
  @media (max-width: 40rem) {
    .spec div {
      grid-template-columns: minmax(0, 1fr);
      gap: 0.25em;
    }
  }
  dd {
    margin: 0;
  }
  a {
    display: inline-flex;
    align-items: center;
    min-height: 44px;
    min-width: 44px;
    max-width: 100%;
    overflow-wrap: anywhere;
  }
</style>
