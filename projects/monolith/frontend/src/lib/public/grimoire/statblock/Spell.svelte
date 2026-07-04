<script>
  import { normalizeBlocks, scalarToText } from "$lib/public/grimoire/format.js";

  let { data } = $props();

  const strap = $derived(
    data.level === 0 || data.level === "0"
      ? [data.school, "cantrip"].filter(Boolean).join(" ")
      : ["Level", data.level, data.school].filter((x) => x != null && x !== "")
          .join(" "),
  );

  const grid = $derived(
    [
      ["Casting Time", data.casting_time],
      ["Range", data.range],
      ["Components", data.components],
      ["Duration", data.duration],
    ].filter(([, v]) => v != null && v !== ""),
  );

  const classes = $derived(
    data.classes
      ? Array.isArray(data.classes)
        ? data.classes.join(", ")
        : scalarToText(data.classes)
      : "",
  );

  const description = $derived(normalizeBlocks(data.description));
</script>

<article class="card-hard frame">
  <h2 class="display name">{data.name}</h2>
  {#if strap}<p class="eyebrow strap">{strap}</p>{/if}

  <hr class="rule" />

  {#if grid.length}
    <dl class="meta">
      {#each grid as [label, value] (label)}
        <div>
          <dt class="eyebrow">{label}</dt>
          <dd>{value}</dd>
        </div>
      {/each}
    </dl>
  {/if}

  {#if classes}
    <p class="classes">
      <span class="eyebrow classes-label">Classes</span> {classes}
    </p>
  {/if}

  {#if description.length}
    <hr class="rule" />
    <div class="body">
      {#each description as p, i (i)}
        <p class="para">
          {#if p.name}<strong>{p.name}.</strong>{/if}
          {p.text}
        </p>
      {/each}
    </div>
  {/if}
</article>

<style>
  .frame {
    padding: clamp(20px, 4vw, 32px);
    border-top: 4px solid var(--blue);
    max-width: 640px;
  }

  .name {
    font-size: clamp(28px, 5vw, 40px);
  }

  .strap {
    margin-top: 4px;
  }

  .rule {
    border: none;
    border-top: 2px solid var(--rule);
    margin: 16px 0;
  }

  .meta {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px 24px;
  }

  .meta dd {
    font-weight: 700;
    font-size: 15px;
  }

  .classes {
    margin-top: 14px;
    font-size: 15px;
  }

  .classes-label {
    margin-right: 6px;
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .para {
    line-height: 1.65;
  }

  @media (max-width: 480px) {
    .meta {
      grid-template-columns: 1fr;
    }
  }
</style>
