<script>
  import { SERIES_ROLES, requireText } from "./contracts.js";

  // Caller labels are presentation text. Contract order and shapes never vary.
  let { entries = SERIES_ROLES, label = "Chart series" } = $props();
  const checkedLabel = $derived(requireText(label, "legend label"));
  const ordered = $derived.by(() => {
    if (!Array.isArray(entries))
      throw new TypeError("legend entries must be an array");
    const labels = new Map();
    for (const entry of entries) {
      if (!SERIES_ROLES.some(({ id }) => id === entry.id))
        throw new RangeError(`Unknown series role: ${entry.id}`);
      if (labels.has(entry.id))
        throw new RangeError(`Duplicate series role: ${entry.id}`);
      labels.set(entry.id, requireText(entry.label, "series label"));
    }
    return SERIES_ROLES.filter(({ id }) => labels.has(id)).map((role) => ({
      ...role,
      label: labels.get(role.id),
    }));
  });
</script>

<ul aria-label={checkedLabel}>
  {#each ordered as entry (entry.id)}
    <li data-series-role={entry.id} data-marker={entry.marker}>
      <svg
        viewBox="0 0 20 20"
        aria-hidden="true"
        style={`fill: var(${entry.role}, var(--ds-ink))`}
      >
        {#if entry.marker === "circle"}<circle cx="10" cy="10" r="7" />
        {:else if entry.marker === "square"}<rect
            x="3"
            y="3"
            width="14"
            height="14"
          />
        {:else if entry.marker === "triangle"}<path d="M10 2 L18 18 H2 Z" />
        {:else if entry.marker === "diamond"}<path
            d="M10 1 L19 10 L10 19 L1 10 Z"
          />
        {:else}<path d="M7 2 H13 V7 H18 V13 H13 V18 H7 V13 H2 V7 H7 Z" />{/if}
      </svg>
      <span>{entry.label} ({entry.marker})</span>
    </li>
  {/each}
</ul>

<style>
  ul {
    list-style: none;
    margin: 0;
    padding: 0;
    color: var(--ds-ink);
    font-family: var(--ds-font-body);
  }
  li {
    display: flex;
    align-items: baseline;
    gap: var(--ds-space-sm);
    padding-block: var(--ds-space-xs);
    min-width: 0;
  }
  span {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  svg {
    width: 1em;
    height: 1em;
    flex: none;
  }
</style>
