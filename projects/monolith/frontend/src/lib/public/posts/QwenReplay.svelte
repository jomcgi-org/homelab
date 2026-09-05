<script>
  import recording from "./qwen-replay.json";

  const turn = recording.turns[0];
  // A visual starting layout, not measured pre-request routing activity.
  const initialSample = turn.samples.find((item) => item.tiers);
  const initialPlacement = {
    hotHits: initialSample.tiers.hotExperts,
    warmHits: initialSample.tiers.warmExperts,
    coldHits: initialSample.tiers.coldExperts,
    unknownHits: 0,
    totalHits: initialSample.tiers.totalExperts,
  };
  let lastActivity;
  const decodeAt = turn.events[0].at;
  // Exclude intervals that overlap prefill, including the boundary sample.
  const decodeSamples = turn.samples.filter(
    (item, index) => index > 0 && turn.samples[index - 1].at >= decodeAt,
  );
  const routingAt = decodeSamples[0].at;
  const routingDuration = turn.durationMs - routingAt;
  const prefillSamples = turn.statsSamples.filter((item) => item.at < decodeAt);
  const prefillPeak = Math.max(
    1,
    ...prefillSamples.map((item) => item.prefillTps ?? 0),
  );
  const prefillScale = Math.ceil(prefillPeak / 100) * 100;
  const history = decodeSamples.map((item) => {
    if (item.activity?.totalHits > 0) lastActivity = item.activity;
    return { ...item, activity: item.unavailable ? null : lastActivity };
  });
  let position = $state(0);
  let playing = $state(false);
  let highlighted = $state(null);
  let answer = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .map((event) => event.content)
      .join(""),
  );
  let sample = $derived(turn.samples.findLast((item) => item.at <= position));
  let statsSample = $derived(
    turn.statsSamples.findLast((item) => item.at <= position),
  );
  let stats = $derived(statsSample?.unavailable ? null : statsSample);
  let routingSample = $derived(
    sample?.unavailable
      ? null
      : decodeSamples.findLast(
          (item) => item.at <= position && item.activity?.totalHits > 0,
        ),
  );
  let initial = $derived(position < routingAt);
  let activity = $derived(initial ? initialPlacement : routingSample?.activity);
  let placement = $derived(initial ? initialSample.tiers : sample?.tiers);
  let phase = $derived(
    position >= turn.durationMs
      ? "Complete"
      : position < turn.events[0].at
        ? "Prefill"
        : "Decode",
  );
  const tiers = [
    {
      key: "hot",
      name: "Hot",
      location: "GPU",
      hits: "hotHits",
      bytes: "hotBytes",
      description: "Hot experts stay in GPU slots.",
    },
    {
      key: "warm",
      name: "Warm",
      location: "Pinned RAM",
      hits: "warmHits",
      bytes: "warmBytes",
      description: "Pinned experts transfer over PCIe to the GPU.",
    },
    {
      key: "cold",
      name: "Cold",
      location: "NVMe / CPU",
      hits: "coldHits",
      bytes: "coldBytes",
      description: "Cold experts run on the CPU through the page cache.",
    },
  ];
  const seconds = (ms) => (ms == null ? "--" : (ms / 1000).toFixed(1) + " s");
  const count = (n) => (n == null ? "--" : n.toLocaleString("en-US"));
  const share = (n, total) => (total > 0 ? (100 * n) / total : 0);
  const percent = (n, total) =>
    total > 0 ? share(n, total).toFixed(0) + "%" : "--";
  const gb = (n) => (n == null ? "--" : (n / 1e9).toFixed(1) + " GB");
  function bands(a) {
    let y = 0;
    return [...tiers, { key: "unknown", hits: "unknownHits" }].map((tier) => {
      const height = share(a?.[tier.hits], a?.totalHits);
      const band = { key: tier.key, y, height };
      y += height;
      return band;
    });
  }
  function toggle() {
    if (position >= turn.durationMs) position = 0;
    playing = !playing;
  }
  $effect(() => {
    if (!playing) return;
    const duration = turn.durationMs;
    let previous = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      position = Math.min(duration, position + now - previous);
      previous = now;
      if (position >= duration) playing = false;
    }, 50);
    return () => clearInterval(timer);
  });
</script>

<section class="replay" aria-label="Recorded inference on the RTX 4090">
  <div class="controls">
    <button
      type="button"
      onclick={toggle}
      onpointerdown={(event) => (event.currentTarget.dataset.pointer = "true")}
      onkeydown={(event) => delete event.currentTarget.dataset.pointer}
      onblur={(event) => delete event.currentTarget.dataset.pointer}
      >{playing
        ? "Pause"
        : position >= turn.durationMs
          ? "Replay"
          : "Play"}</button
    >
    <label class="timeline"
      ><span class="sr-only">Recorded time</span><input
        type="range"
        min="0"
        max={turn.durationMs}
        step="1"
        bind:value={position}
        oninput={() => (playing = false)}
        aria-valuetext={seconds(position)}
      /></label
    >
    <span class="time">{seconds(position)}</span>
  </div>
  <div class="instrument">
    <header class="instrument-heading">
      <span
        >{phase === "Prefill"
          ? "Processing the prompt."
          : phase === "Decode"
            ? "Generating one token at a time."
            : "Session complete."}</span
      >
      <span class="phase" data-phase={phase} role="status"><i></i>{phase}</span>
    </header>
    <dl
      class="measurements"
      aria-label="Recorded service throughput and memory"
    >
      <div>
        <dt>Prefill</dt>
        <dd>
          {stats?.prefillTps == null ? "--" : stats.prefillTps.toFixed(1)}
          <small>tok/s</small>
        </dd>
      </div>
      <div>
        <dt>Decode</dt>
        <dd>
          {stats?.decodeTps == null ? "--" : stats.decodeTps.toFixed(1)}
          <small>tok/s</small>
        </dd>
      </div>
      <div>
        <dt>KV pages</dt>
        <dd>
          {count(stats?.kvUsedPages)}
          <small>/ {count(stats?.kvTotalPages)}</small>
        </dd>
      </div>
    </dl>

    <section class="telemetry" aria-label="Expert activity">
      <header class="routing-heading">
        <strong>Expert routing</strong>
        <span
          >{initial
            ? "Initial placement"
            : activity
              ? count(activity.totalHits) + " activations"
              : "Awaiting sample"}</span
        >
      </header>
      <div
        class="activity-bar"
        role="img"
        aria-label={(initial
          ? "Initial expert placement: "
          : "Expert routing: ") +
          tiers
            .map(
              (tier) =>
                tier.name +
                " " +
                percent(activity?.[tier.hits], activity?.totalHits),
            )
            .join(", ")}
      >
        {#each tiers as tier}
          <span
            class={tier.key}
            class:dimmed={highlighted && highlighted !== tier.key}
            style:width={share(activity?.[tier.hits], activity?.totalHits) +
              "%"}
          ></span>
        {/each}
        <span
          class="unknown"
          style:width={share(activity?.unknownHits, activity?.totalHits) + "%"}
        ></span>
      </div>
      <div class="tiers">
        {#each tiers as tier}
          <button
            type="button"
            class={"tier " + tier.key}
            aria-pressed={highlighted === tier.key}
            aria-label={tier.name + ": " + tier.description}
            onclick={() =>
              (highlighted = highlighted === tier.key ? null : tier.key)}
          >
            <span class="tier-label"><i></i>{tier.name}</span>
            <span class="tier-location">{tier.location}</span>
            <strong
              >{percent(activity?.[tier.hits], activity?.totalHits)}
              <small>{initial ? "of experts" : "of routes"}</small></strong
            >
            <span class="capacity">{gb(placement?.[tier.bytes])} capacity</span>
          </button>
        {/each}
      </div>
      {#if highlighted}<p class="caption tier-description">
          {tiers.find((tier) => tier.key === highlighted).description}
        </p>{/if}
      {#if activity?.unknownHits > 0}<p class="caption">
          {percent(activity.unknownHits, activity.totalHits)} unclassified
        </p>{/if}

      <div class="phase-charts">
        <div class="prefill-history">
          <header>Prefill <small>Throughput (tok/s)</small></header>
          <div class="prefill-plot">
            <div class="prefill-axis" aria-hidden="true">
              <span>{prefillScale}</span><span>{prefillScale / 2}</span><span
                >&nbsp;</span
              >
            </div>
            <svg
              viewBox="0 0 300 100"
              preserveAspectRatio="none"
              role="img"
              aria-label="Recorded prefill service throughput"
            >
              <title
                >Service throughput, 0 to {prefillScale} tok/s. Playback interpolates
                between recorded samples.</title
              >
              {#each [0, 50, 100] as y}
                <line
                  x1="0"
                  x2="300"
                  y1={y}
                  y2={y}
                  stroke="var(--line)"
                  stroke-width="1"
                />
              {/each}
              {#each prefillSamples as item, index}
                {@const previous = prefillSamples[index - 1]}
                {#if previous && previous.at <= position && !previous.unavailable && previous.prefillTps != null && !item.unavailable && item.prefillTps != null}
                  {@const progress = Math.min(
                    1,
                    (position - previous.at) / (item.at - previous.at),
                  )}
                  {@const at = previous.at + progress * (item.at - previous.at)}
                  {@const rate =
                    previous.prefillTps +
                    progress * (item.prefillTps - previous.prefillTps)}
                  <line
                    class="prefill-segment"
                    x1={(300 * previous.at) / decodeAt}
                    y1={100 - (100 * previous.prefillTps) / prefillScale}
                    x2={(300 * at) / decodeAt}
                    y2={100 - (100 * rate) / prefillScale}
                    stroke="var(--ink)"
                    stroke-width="2"
                    stroke-linecap="round"
                  />
                {/if}
                {#if item.at <= position && !item.unavailable && item.prefillTps != null}
                  <circle
                    cx={(300 * item.at) / decodeAt}
                    cy={100 - (100 * item.prefillTps) / prefillScale}
                    r="2"
                    fill="var(--ink)"
                  />
                {/if}
              {/each}
            </svg>
          </div>
          <div class="history-labels">
            <span class="origin">0</span><span>{seconds(decodeAt)}</span>
          </div>
        </div>
        <div class="routing-history">
          <header>Experts <small>decode routing</small></header>
          <svg
            viewBox="0 0 700 100"
            preserveAspectRatio="none"
            role="img"
            aria-label="Expert routing from fully decoded sample intervals only"
          >
            <title>Routing mix over recorded time</title>
            {#each history as item, index}
              {@const start = item.at}
              {@const end = Math.min(
                position,
                history[index + 1]?.at ?? turn.durationMs,
              )}
              {#if item.at <= position}
                {#if item.unavailable || !item.activity?.totalHits}
                  <rect
                    class="unknown"
                    x={(700 * (start - routingAt)) / routingDuration}
                    y="0"
                    width={(700 * (end - start)) / routingDuration}
                    height="100"
                    opacity="0.25"
                  />
                {:else}
                  {#each bands(item.activity) as band}
                    <rect
                      class={band.key}
                      class:dimmed={highlighted && highlighted !== band.key}
                      x={(700 * (start - routingAt)) / routingDuration}
                      y={band.y}
                      width={(700 * (end - start)) / routingDuration}
                      height={band.height}
                    />
                  {/each}
                {/if}
              {/if}
            {/each}
            <line
              x1={(700 * Math.max(0, position - routingAt)) / routingDuration}
              x2={(700 * Math.max(0, position - routingAt)) / routingDuration}
              y1="0"
              y2="100"
              stroke="var(--ink)"
              stroke-width="3"
            />
          </svg>
          <div class="history-labels">
            <span>{seconds(routingAt)}</span><span
              >{seconds(turn.durationMs)}</span
            >
          </div>
        </div>
      </div>
    </section>
  </div>

  <div class="conversation">
    <details class="prompt">
      <summary>Prompt</summary>
      <pre>{turn.prompt}</pre>
    </details>
    <!-- Keyboard users need to focus this region to scroll a long answer. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="answer" tabindex="0" role="region" aria-label="Recorded answer">
      <p>{answer || "Waiting for the first token..."}</p>
    </div>
    {#if turn.note && position >= turn.durationMs}<p class="caption">
        {turn.note}
      </p>{/if}
  </div>
</section>

<style>
  .replay {
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  .replay .caption {
    color: var(--ink-2);
    font-size: 0.75rem;
    line-height: 1.5;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    margin: 0.9rem 0;
  }
  .controls button {
    width: 5.5rem;
    flex-shrink: 0;
  }
  .controls :global(button[data-pointer]:focus) {
    outline: none;
  }
  button {
    font: inherit;
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.5rem 0.65rem;
    border-radius: 3px;
    cursor: pointer;
  }
  button[aria-pressed="true"] {
    background: var(--band);
    border-color: var(--accent-ink);
  }
  button:focus-visible,
  input:focus-visible,
  summary:focus-visible,
  .answer:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .instrument {
    margin-inline: -1rem;
    border-block: 1px solid var(--stroke);
  }
  .instrument-heading,
  .routing-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .instrument-heading {
    padding: 0.8rem 1rem;
    border-bottom: 1px solid var(--stroke);
    font-size: 0.9rem;
  }
  .phase {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    font: 0.7rem var(--font-code);
  }
  .phase i {
    width: 0.45rem;
    height: 0.45rem;
    border-radius: 50%;
    background: var(--ink-3);
  }
  .phase[data-phase="Decode"] i {
    background: var(--ok);
  }
  .phase[data-phase="Prefill"] i {
    background: var(--accent);
  }
  .measurements {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 0;
    border-bottom: 1px solid var(--stroke);
  }
  .measurements > div {
    padding: 1rem;
  }
  .measurements > div + div {
    border-left: 1px solid var(--line);
  }
  dt {
    font-size: 0.7rem;
    color: var(--ink-2);
  }
  dd {
    margin: 0.4rem 0 0;
    font: 1.65rem var(--font-code);
    overflow-wrap: anywhere;
  }
  dd small {
    font-size: 0.7rem;
    color: var(--ink-2);
  }
  .telemetry {
    padding: 1rem;
  }
  .routing-heading {
    font-size: 0.8rem;
    margin-bottom: 0.8rem;
  }
  .routing-heading > span {
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.7rem;
  }
  .activity-bar {
    display: flex;
    height: 1.6rem;
    background: var(--band);
    overflow: hidden;
  }
  .hot {
    --tier-color: var(--replay-hot);
  }
  .warm {
    --tier-color: var(--replay-warm);
  }
  .cold {
    --tier-color: var(--replay-cold);
  }
  .unknown {
    --tier-color: var(--ink-3);
  }
  .activity-bar > span {
    background: var(--tier-color);
  }
  .tiers {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 0;
    margin: 0.8rem -1rem 1rem;
    border-block: 1px solid var(--line);
  }
  .tier {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    min-width: 0;
    text-align: left;
    padding: 0.75rem 1rem;
    border: 0;
    border-radius: 0;
    background: transparent;
  }
  .tier[aria-pressed="true"] {
    box-shadow: inset 0 3px var(--tier-color);
    background: var(--band);
  }
  .tier + .tier {
    border-left: 1px solid var(--line);
  }
  .dimmed {
    opacity: 0.15;
  }
  .tier-label {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.3rem;
    font-size: 0.75rem;
    font-weight: 600;
  }
  .tier-label i {
    width: 0.5rem;
    height: 0.5rem;
    background: var(--tier-color);
  }
  .tier-location,
  .capacity {
    font-size: 0.65rem;
    color: var(--ink-2);
  }
  .tier > strong {
    margin-block: 0.25rem;
    font: 1.65rem var(--font-code);
    color: color-mix(in srgb, var(--tier-color) 45%, var(--ink));
  }
  .tier > strong small {
    display: block;
    font: 0.65rem var(--font-ui);
    color: var(--ink-2);
  }
  .phase-charts {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 2fr);
    gap: 1rem;
  }
  .phase-charts header {
    font-size: 0.75rem;
    margin-bottom: 0.5rem;
  }
  .phase-charts header small {
    display: block;
    font-size: 0.65rem;
    color: var(--ink-2);
  }
  .phase-charts svg {
    display: block;
    width: 100%;
    height: 4rem;
    background: var(--band);
  }
  .prefill-plot {
    display: flex;
    gap: 0.3rem;
  }
  .prefill-plot svg {
    flex: 1;
    min-width: 0;
  }
  .prefill-axis {
    width: 1.6rem;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding-block: 0.2rem;
    font: 0.6rem var(--font-code);
    color: var(--ink-2);
    text-align: right;
  }
  .prefill-history .history-labels {
    margin-left: 1.9rem;
  }
  .origin {
    transform: translateX(-50%);
  }
  .routing-history rect {
    fill: var(--tier-color);
  }
  .history-labels {
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.4rem;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
    margin-top: 0.4rem;
  }
  .controls {
    font-size: 0.75rem;
  }
  .timeline {
    flex: 1;
    min-width: 6rem;
    display: flex;
    align-items: center;
  }
  .timeline input {
    width: 100%;
    min-width: 0;
    accent-color: var(--accent-ink);
  }
  .time {
    min-width: 4rem;
    text-align: right;
    font-family: var(--font-code);
  }
  .conversation {
    margin-inline: -1rem;
    padding-inline: 1rem;
    border-block: 1px solid var(--stroke);
  }
  summary {
    cursor: pointer;
    line-height: 1.5;
    overflow-wrap: anywhere;
    font-size: 0.75rem;
    color: var(--ink-2);
  }
  .prompt {
    padding: 0.65rem 0;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 12rem;
    overflow-y: auto;
    font: 0.8rem var(--font-ui);
  }
  .answer {
    max-height: 7rem;
    min-height: 3rem;
    overflow-y: auto;
    padding-bottom: 0.7rem;
  }
  .replay .answer p {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    font-size: 0.8rem;
    line-height: 1.6;
    margin: 0;
    color: var(--ink-2);
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    overflow: hidden;
    clip-path: inset(50%);
  }
  @media (max-width: 540px) {
    .measurements > div {
      padding: 0.75rem 0.5rem;
    }
    dd {
      font-size: 1.3rem;
    }
    .telemetry {
      padding: 0.75rem;
    }
    .tier > strong {
      font-size: 1.3rem;
    }
    .instrument-heading {
      padding: 0.75rem;
    }
    .time {
      min-width: 3rem;
    }
  }
</style>
