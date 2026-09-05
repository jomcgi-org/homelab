<script>
  import recording from "./qwen-replay.json";

  let selected = $state(0);
  let position = $state(recording.turns[0].durationMs);
  let playing = $state(false);
  let speed = $state(1);
  let highlighted = $state(null);
  let turn = $derived(recording.turns[selected]);
  let answer = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .map((event) => event.content)
      .join(""),
  );
  let sample = $derived(turn.samples.findLast((item) => item.at <= position));
  let routingSample = $derived(
    sample?.unavailable
      ? null
      : turn.samples.findLast(
          (item) => item.at <= position && item.activity?.totalHits > 0,
        ),
  );
  let activity = $derived(routingSample?.activity);
  let phase = $derived(
    position >= turn.durationMs
      ? "Complete"
      : position < turn.events[0].at
        ? "Waiting"
        : "Generating",
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
  function choose(index) {
    playing = false;
    selected = index;
    position = recording.turns[index].durationMs;
  }
  function toggle() {
    if (position >= turn.durationMs) position = 0;
    playing = !playing;
  }
  $effect(() => {
    if (!playing) return;
    const rate = Number(speed);
    const duration = turn.durationMs;
    let previous = performance.now();
    const timer = setInterval(() => {
      const now = performance.now();
      position = Math.min(duration, position + (now - previous) * rate);
      previous = now;
      if (position >= duration) playing = false;
    }, 50);
    return () => clearInterval(timer);
  });
</script>

<section class="replay" aria-label="Recorded inference on the RTX 4090">
  <nav class="turns" aria-label="Recorded turns">
    {#each recording.turns as item, index}
      <button
        type="button"
        aria-pressed={selected === index}
        onclick={() => choose(index)}>{index + 1}. {item.title}</button
      >
    {/each}
  </nav>

  <div class="instrument">
    <header class="instrument-heading">
      <strong>RTX 4090 <span>· Qwen 125B</span></strong>
      <span class="phase" data-phase={phase} role="status"><i></i>{phase}</span>
    </header>
    <dl
      class="measurements"
      aria-label="Measurements for the complete recorded turn"
    >
      <div>
        <dt>Decode · turn average</dt>
        <dd>
          {turn.metrics.tokensPerSecond == null
            ? "--"
            : turn.metrics.tokensPerSecond.toFixed(1)} <small>tok/s</small>
        </dd>
      </div>
      <div>
        <dt>First token</dt>
        <dd>{seconds(turn.metrics.ttftMs)}</dd>
      </div>
      <div>
        <dt>Output tokens</dt>
        <dd>{count(turn.metrics.completionTokens)}</dd>
      </div>
    </dl>

    <section class="telemetry" aria-label="Expert activity">
      <header class="routing-heading">
        <strong>Expert routing</strong>
        <span
          >{activity
            ? count(activity.totalHits) + " activations"
            : "Awaiting sample"}</span
        >
      </header>
      <div
        class="activity-bar"
        role="img"
        aria-label={"Expert routing: " +
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
            <span class="tier-label"
              ><i></i>{tier.name} <small>{tier.location}</small></span
            >
            <strong
              >{percent(activity?.[tier.hits], activity?.totalHits)}</strong
            >
            <span class="capacity"
              >{gb(sample?.tiers?.[tier.bytes])} capacity</span
            >
          </button>
        {/each}
      </div>
      {#if highlighted}<p class="caption tier-description">
          {tiers.find((tier) => tier.key === highlighted).description}
        </p>{/if}
      {#if activity?.unknownHits > 0}<p class="caption">
          {percent(activity.unknownHits, activity.totalHits)} unclassified
        </p>{/if}

      <div class="routing-history">
        <svg
          viewBox="0 0 700 100"
          preserveAspectRatio="none"
          role="img"
          aria-label="Recorded routing mix over time: hot, warm, and cold expert activations"
        >
          <title>Routing mix over recorded time</title>
          {#each turn.samples as item, index}
            {@const start = index ? turn.samples[index - 1].at : 0}
            {#if item.at <= position}
              {#if item.unavailable || !item.activity?.totalHits}
                <rect
                  class="unknown"
                  x={(700 * start) / turn.durationMs}
                  y="0"
                  width={(700 * (item.at - start)) / turn.durationMs}
                  height="100"
                  opacity="0.25"
                />
              {:else}
                {#each bands(item.activity) as band}
                  <rect
                    class={band.key}
                    class:dimmed={highlighted && highlighted !== band.key}
                    x={(700 * start) / turn.durationMs}
                    y={band.y}
                    width={(700 * (item.at - start)) / turn.durationMs}
                    height={band.height}
                  />
                {/each}
              {/if}
            {/if}
          {/each}
          <line
            x1={(700 * position) / turn.durationMs}
            x2={(700 * position) / turn.durationMs}
            y1="0"
            y2="100"
            stroke="var(--ink)"
            stroke-width="3"
          />
        </svg>
        <div class="history-labels">
          <span>0 s</span><span>Routing mix over time</span><span
            >{seconds(turn.durationMs)}</span
          >
        </div>
      </div>
      <div class="sample-status">
        <span
          >{routingSample
            ? "Sample " + seconds(routingSample.at)
            : "No telemetry sample available"}</span
        >
        <span
          >KV {count(sample?.kvUsedPages)} / {count(sample?.kvTotalPages)} pages</span
        >
        <span>{count(sample?.activeRequests)} in flight</span>
      </div>
    </section>
  </div>

  <div class="controls">
    <button type="button" onclick={toggle}
      >{playing
        ? "Pause"
        : position >= turn.durationMs
          ? "Replay turn"
          : "Play"}</button
    >
    <label
      >Speed <select bind:value={speed}
        ><option value={1}>1×</option><option value={4}>4×</option><option
          value={16}>16×</option
        ></select
      ></label
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

  <div class="conversation">
    <details class="prompt">
      <summary>Prompt · {turn.title}</summary>
      <pre>{turn.prompt}</pre>
    </details>
    <!-- Keyboard users need to focus this region to scroll a long answer. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="answer" tabindex="0" role="region" aria-label="Recorded answer">
      <p>{answer || "Waiting for the first token..."}</p>
    </div>
    {#if selected === 2 && position >= turn.durationMs}
      <p class="caption">
        Correction: GLM's 99.8% hit rate covers disk-tier routes, not all
        routes. Pinned host layers still transfer over PCIe.
      </p>
    {/if}
  </div>

  <details class="recording-notes">
    <summary
      >Recording details · {recording.recordedAt.slice(0, 10)} UTC</summary
    >
    <p class="caption">
      {recording.hardware}. Thinking off. Warm service with other traffic.
    </p>
    <p class="caption">
      Routing and capacity are estimates from the live demo. Gaps indicate
      missing or empty samples. Measurements use server token counts and
      recorded timings, independent of playback speed.
    </p>
    <p class="caption">
      Build <a
        href={"https://github.com/jomcgi-org/freetoken-fork/commit/" +
          recording.build}>{recording.build.slice(0, 7)}</a
      >
      · {count(turn.usage?.prompt_tokens)} prompt tokens · Finish: {turn.finishReason}
    </p>
  </details>
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
  .turns,
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    margin: 0.9rem 0;
  }
  button,
  select {
    font: inherit;
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.5rem 0.65rem;
    border-radius: 3px;
    cursor: pointer;
  }
  .turns button {
    font-size: 0.8rem;
  }
  button[aria-pressed="true"] {
    background: var(--band);
    border-color: var(--accent-ink);
  }
  button:focus-visible,
  select:focus-visible,
  input:focus-visible,
  summary:focus-visible,
  .answer:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
  }
  .instrument {
    border: 1px solid var(--stroke);
    background: var(--card-bg);
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
  .instrument-heading strong span {
    color: var(--ink-2);
    font-weight: 400;
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
  .phase[data-phase="Generating"] i {
    background: var(--ok);
  }
  .phase[data-phase="Waiting"] i {
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
    --tier-color: var(--tone-hot);
  }
  .warm {
    --tier-color: var(--tone-ram);
  }
  .cold {
    --tier-color: var(--tone-disk);
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
    gap: 0.65rem;
    margin: 1rem 0;
  }
  .tier {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    min-width: 0;
    text-align: left;
    padding: 0.5rem;
    border-color: transparent;
    background: transparent;
  }
  .tier[aria-pressed="true"] {
    border-color: var(--tier-color);
    background: var(--band);
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
  }
  .tier-label i {
    width: 0.5rem;
    height: 0.5rem;
    background: var(--tier-color);
  }
  .tier-label small,
  .capacity {
    font-size: 0.65rem;
    color: var(--ink-2);
  }
  .tier > strong {
    font: 1.6rem var(--font-code);
    color: var(--tier-color);
  }
  .routing-history svg {
    display: block;
    width: 100%;
    height: 4rem;
    background: var(--band);
  }
  .routing-history rect {
    fill: var(--tier-color);
  }
  .history-labels,
  .sample-status {
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 0.4rem;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
    margin-top: 0.4rem;
  }
  .sample-status {
    border-top: 1px solid var(--line);
    padding-top: 0.65rem;
    margin-top: 0.8rem;
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
  .recording-notes {
    margin-top: 0.8rem;
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
