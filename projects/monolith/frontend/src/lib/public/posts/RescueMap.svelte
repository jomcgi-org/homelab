<script>
  import { rescueEvents } from "./rescue-events.js";
  let { answer = "", complete = false } = $props();
  let events = $derived(rescueEvents(answer, complete));
  let selected = $state(null);
  let active = $derived(events.find((e) => e.id === selected) ?? events.at(-1));
  const locations = [
    [738, 248],
    [617, 289],
    [488, 275],
    [359, 255],
    [231, 232],
    [105, 200],
  ];
</script>

<div class="rescue-map">
  <svg
    viewBox="0 0 820 370"
    role="group"
    aria-label="Apollo 13 rescue path around the Moon and back to Earth"
  >
    <defs
      ><marker
        id="rescue-arrow"
        viewBox="0 0 10 10"
        refX="8"
        refY="5"
        markerWidth="5"
        markerHeight="5"
        orient="auto"
        ><path d="M 0 0 L 10 5 L 0 10 z" fill="var(--tone-ram)" /></marker
      ></defs
    >
    <circle cx="100" cy="170" r="44" class="earth" />
    <path
      d="M75 137 l24 11 -8 21 24 8 10 24 -27 8 -16 -26 -22 -4"
      class="land"
    />
    <text x="100" y="105" text-anchor="middle">Earth</text>
    <circle cx="717" cy="160" r="28" class="moon" />
    <circle cx="710" cy="150" r="7" class="crater" /><circle
      cx="729"
      cy="169"
      r="5"
      class="crater"
    />
    <text x="717" y="105" text-anchor="middle">Moon</text>
    <path d="M140 155 C330 20 610 40 740 135" class="outbound" />
    <text x="375" y="65" class="quiet">Lunar landing aborted</text>
    <path
      d="M740 135 C825 220 700 310 550 285 S250 245 105 200"
      class="return-guide"
    />
    <path
      d="M740 135 C825 220 700 310 550 285 S250 245 105 200"
      class="route"
      pathLength="100"
      style={`stroke-dasharray:${(events.length / 6) * 100} 100`}
    />
    {#each events as event, i}
      {@const point = locations[i]}
      <g
        role="button"
        tabindex="0"
        aria-label={event.title}
        aria-pressed={active?.id === event.id}
        onclick={() => (selected = event.id)}
        onkeydown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selected = event.id;
          }
        }}
        class:active={active?.id === event.id}
        class:failure={i === 0}
        style={`--event-tone:var(--tone-${i === 0 ? "disk" : i === 3 ? "hot" : "ram"})`}
      >
        <circle cx={point[0]} cy={point[1]} r="9" class="waypoint" />
        <circle cx={point[0]} cy={point[1]} r="17" class="halo" />
      </g>
    {/each}
    {#if events.length}
      {@const point = locations[events.length - 1]}
      <g style={`transform:translate(${point[0]}px,${point[1]}px)`} class="ship"
        ><path d="M-8 -6 L12 0 L-8 6 L-4 0 Z" /></g
      >
    {/if}
  </svg>
  <div class="event-strip" aria-label="Rescue events">
    {#each events as event}
      <button
        class:chosen={active?.id === event.id}
        onclick={() => (selected = event.id)}
        aria-pressed={active?.id === event.id}>{event.title}</button
      >
    {/each}
  </div>
  <div class="event-detail" aria-live="polite">
    {#if active}<p>{active.detail}</p>{:else}<p class="quiet">
        Building the rescue map…
      </p>{/if}
  </div>
  <details>
    <summary>Model output</summary>
    <pre>{answer}</pre>
  </details>
</div>

<style>
  .rescue-map {
    min-width: 0;
  }
  svg {
    display: block;
    width: 100%;
    height: auto;
  }
  svg text {
    fill: var(--ink);
    font: 16px var(--font-code);
  }
  .earth {
    fill: color-mix(in srgb, var(--tone-gpu) 16%, var(--sheet));
    stroke: var(--tone-gpu);
    stroke-width: 2;
  }
  .land {
    fill: var(--tone-ram);
    opacity: 0.65;
  }
  .moon {
    fill: var(--sheet);
    stroke: var(--ink-2);
    stroke-width: 2;
  }
  .crater {
    fill: var(--ink-3);
    opacity: 0.25;
  }
  .outbound {
    fill: none;
    stroke: var(--ink-3);
    stroke-width: 1.5;
    stroke-dasharray: 4 7;
  }
  .route {
    fill: none;
    stroke: var(--tone-ram);
    stroke-width: 2;
    transition: stroke-dasharray 0.8s ease;
  }
  .quiet {
    color: var(--ink-2);
    fill: var(--ink-2);
    font-size: 12px;
  }
  g[role="button"] {
    cursor: pointer;
  }
  g[role="button"]:focus-visible .waypoint {
    stroke-width: 5;
  }
  .waypoint {
    fill: var(--sheet);
    stroke: var(--event-tone);
    stroke-width: 3;
  }
  .halo {
    fill: none;
    stroke: var(--event-tone);
    opacity: 0;
  }
  .active .halo {
    animation: pulse 2s ease-out infinite;
  }
  .return-guide {
    fill: none;
    stroke: var(--tone-ram);
    opacity: 0.15;
    stroke-width: 2;
  }
  .ship {
    transition: transform 0.8s ease;
  }
  .ship path {
    fill: var(--tone-gpu);
    stroke: var(--sheet);
    stroke-width: 2;
  }
  .event-strip {
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
  }
  button {
    border: 1px solid var(--rule);
    background: var(--sheet);
    color: var(--ink-2);
    padding: 0.45rem 0.6rem;
    font: 0.75rem var(--font-code);
    cursor: pointer;
  }
  button.chosen {
    color: var(--tone-ram);
    border-color: var(--tone-ram);
    background: color-mix(in srgb, var(--tone-ram) 9%, var(--sheet));
  }
  button:focus-visible {
    outline: 2px solid var(--tone-gpu);
    outline-offset: 3px;
  }
  .event-detail {
    min-height: 4.5rem;
    padding: 0.6rem 0;
  }
  .event-detail p {
    margin: 0;
    line-height: 1.5;
  }
  details {
    color: var(--ink-2);
    font: 0.7rem var(--font-code);
  }
  summary {
    cursor: pointer;
  }
  pre {
    max-height: 15rem;
    overflow: auto;
    white-space: pre-wrap;
  }
  @keyframes pulse {
    from {
      opacity: 0.7;
      r: 12;
    }
    to {
      opacity: 0;
      r: 25;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .route,
    .ship {
      transition: none;
    }
    .active .halo {
      animation: none;
    }
    .active .halo {
      opacity: 0.5;
    }
  }
  @media (max-width: 600px) {
    svg {
      min-height: 210px;
    }
    svg text {
      font-size: 20px;
    }
    .event-detail {
      min-height: 6rem;
    }
  }
</style>
