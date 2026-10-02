<script>
  // One metric, one panel, best model first: the Artificial Analysis small
  // multiple, drawn as a keyed technical table. Horizontal bars so the model
  // names read flat instead of slanting under the axis. Bars start at zero on
  // a linear scale even for cost: Claude rents at roughly a hundred times the
  // budget models, and a log bar would hide exactly that.
  import { METRICS, providerSlot, shortName, sortByMetric } from "./model.js";

  let {
    models = [],
    metric = "hard",
    hot = null,
    onhover = () => {},
  } = $props();

  const cfg = $derived(METRICS[metric]);
  const rows = $derived(sortByMetric(models, metric));
  const max = $derived(
    Math.max(metric === "hard" ? 1 : 0, ...rows.map((m) => cfg.get(m) ?? 0)) ||
      1,
  );
</script>

<section class="panel bars" aria-label={cfg.label}>
  <header class="panel-head">
    <span class="t">{cfg.label}</span>
    <span class="u">{cfg.unit}</span>
    <span class="b">{cfg.better} is better</span>
  </header>
  <ol class="panel-body" class:hovering={hot}>
    {#each rows as m (m.id)}
      {@const v = cfg.get(m) ?? 0}
      {@const note = cfg.note(m)}
      <li
        data-model={m.id}
        class:hot={hot === m.id}
        onmouseenter={() => onhover(m.id)}
        onmouseleave={() => onhover(null)}
        title={`${shortName(m)}: ${cfg.fmt(v)}${note ? ` (${note})` : ""}`}
      >
        <span class="name"
          ><i class="sw" data-slot={providerSlot(m.id)}></i>{shortName(m)}</span
        >
        <span class="track" data-slot={providerSlot(m.id)}
          ><i style={`width:${(v / max) * 100}%`}></i></span
        >
        <span class="val num"
          >{cfg.fmt(v)}{#if note}<small>{note}</small>{/if}</span
        >
      </li>
    {/each}
  </ol>
  {#if cfg.candidatesOnly && rows.length < models.length}
    <p class="omit">Claude omitted (different harness).</p>
  {/if}
</section>

<style>
  ol {
    margin: 0;
    list-style: none;
  }

  li {
    display: grid;
    grid-template-columns: minmax(0, 13em) minmax(0, 1fr) 7.5em;
    gap: 0.7em;
    align-items: center;
    padding: 0.2em 0;
    font-size: 0.8rem;
  }

  li + li {
    border-top: 1px solid var(--line);
  }

  .name {
    display: flex;
    gap: 0.45em;
    align-items: center;
    min-width: 0;
    overflow: hidden;
    color: var(--ink);
    font-family: var(--font-code);
    font-size: 0.72rem;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* The track is the axis: a hairline at zero, so a $0 bar still has a
     visible origin rather than an empty cell. */
  .track {
    position: relative;
    height: 0.8em;
    border-left: 1px solid var(--ink);
  }

  .track i {
    display: block;
    height: 100%;
    background: var(--c, var(--prov-other));
  }

  .val {
    font-family: var(--font-code);
    font-size: 0.74rem;
    text-align: right;
    white-space: nowrap;
  }

  .val small {
    margin-left: 0.45em;
    color: var(--ink-2);
    font-size: 0.9em;
  }

  .omit {
    margin: 0;
    padding: 0 0.8em 0.6em;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.66rem;
  }

  @media (max-width: 520px) {
    li {
      grid-template-columns: minmax(0, 8em) minmax(0, 1fr) 6em;
    }
  }
</style>
