<script>
  import {
    normalizeBlocks,
    scalarToText,
  } from "$lib/public/grimoire/format.js";

  let { data } = $props();

  const strap = $derived(
    data.level === 0 || data.level === "0"
      ? [data.school, "cantrip"].filter(Boolean).join(" ")
      : ["Level", data.level, data.school]
          .filter((x) => x != null && x !== "")
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

<article class="spell grim-paper">
  <h2 class="grim-title name">{data.name}</h2>
  {#if strap}<p class="grim-smallcaps strap">{strap}</p>{/if}

  <hr class="grim-rule" />

  {#if grid.length}
    <dl class="meta">
      {#each grid as [label, value] (label)}
        <div>
          <dt>{label}</dt>
          <dd>{value}</dd>
        </div>
      {/each}
    </dl>
  {/if}

  {#if classes}
    <p class="classes"><span class="classes-label">Classes:</span> {classes}</p>
  {/if}

  {#if description.length}
    <hr class="grim-rule" />
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
  .spell {
    padding: clamp(1rem, 3vw, 1.5rem);
    border: 1px solid var(--stroke);
    border-top: 1px solid var(--accent-ink);
    max-width: 40rem;
  }

  .name {
    font-size: clamp(1.4rem, 4vw, 1.9rem);
    color: var(--accent-ink);
  }

  .strap {
    font-size: 0.85rem;
    color: var(--ink-2);
    font-style: italic;
  }

  .meta {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0.5rem 1.25rem;
  }

  .meta dt {
    font-family: var(--font-code);
    font-size: 0.58rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-2);
  }

  .meta dd {
    font-family: var(--font-ui);
    font-weight: 700;
    font-size: 0.95rem;
  }

  .classes {
    margin-top: 0.6rem;
    font-family: var(--font-ui);
    font-size: 0.9rem;
  }

  .classes-label {
    font-family: var(--font-code);
    font-size: 0.6rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-2);
  }

  .body {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .para {
    font-family: var(--font-ui);
    font-size: 0.95rem;
    line-height: 1.55;
    color: var(--ink);
  }

  @media (max-width: 480px) {
    .meta {
      grid-template-columns: 1fr;
    }
  }
</style>
