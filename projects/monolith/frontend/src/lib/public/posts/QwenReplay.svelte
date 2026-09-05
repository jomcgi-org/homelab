<script>
  import recording from "./qwen-replay.json";

  let selected = $state(0);
  let position = $state(recording.turns[0].durationMs);
  let playing = $state(false);
  let speed = $state(1);
  let turn = $derived(recording.turns[selected]);
  let answer = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .map((event) => event.content)
      .join(""),
  );
  let sample = $derived(
    turn.samples.findLast((sample) => sample.at <= position),
  );
  let activity = $derived(sample?.unavailable ? null : sample?.activity);
  let phase = $derived(
    position >= turn.durationMs
      ? "Complete"
      : position < turn.events[0].at
        ? "Waiting for first token"
        : "Generating",
  );
  const seconds = (ms) =>
    ms == null ? "Unavailable" : `${(ms / 1000).toFixed(1)} s`;
  const count = (n) => (n == null ? "Unavailable" : n.toLocaleString("en-US"));
  const percent = (n, total) =>
    total > 0 ? `${((100 * n) / total).toFixed(0)}%` : "Unavailable";

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

<section class="replay" aria-label="Recorded conversation on the RTX 4090">
  <p class="caption">
    Recorded {recording.recordedAt.slice(0, 10)} UTC · Thinking off
  </p>
  <nav class="turns" aria-label="Recorded turns">
    {#each recording.turns as item, index}
      <button
        type="button"
        aria-pressed={selected === index}
        onclick={() => choose(index)}>{index + 1}. {item.title}</button
      >
    {/each}
  </nav>

  <div class="conversation">
    <details class="prompt">
      <summary
        >{selected === 1
          ? "Prompt: read and summarize the post"
          : `Prompt: ${turn.prompt}`}</summary
      >
      <pre>{turn.prompt}</pre>
    </details>
    <!-- Keyboard users need to focus this region to scroll a long answer. -->
    <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
    <div class="answer" tabindex="0" role="region" aria-label="Recorded answer">
      <p>{answer || "Waiting for the first token..."}</p>
    </div>
  </div>

  {#if selected === 2 && position >= turn.durationMs}
    <p class="caption">
      A correction to this recorded answer: the 99.8% figure applies to routes
      in GLM's disk-tier layers. It does not mean 99.8% of all routes hit GPU
      memory. The pinned host layers still transfer weights over PCIe.
    </p>
  {/if}

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
    <span role="status">{phase}</span>
    <span>{seconds(position)} / {seconds(turn.durationMs)}</span>
  </div>
  <label class="timeline"
    >Recorded time
    <input
      type="range"
      min="0"
      max={turn.durationMs}
      step="1"
      bind:value={position}
      oninput={() => (playing = false)}
      aria-valuetext={seconds(position)}
    />
  </label>

  <dl
    class="measurements"
    aria-label="Measurements for the complete recorded turn"
  >
    <div>
      <dt>First token</dt>
      <dd>{seconds(turn.metrics.ttftMs)}</dd>
    </div>
    <div>
      <dt>Decode</dt>
      <dd>
        {turn.metrics.tokensPerSecond == null
          ? "Unavailable"
          : `${turn.metrics.tokensPerSecond.toFixed(1)} tok/s`}
      </dd>
    </div>
    <div>
      <dt>Output tokens</dt>
      <dd>{count(turn.metrics.completionTokens)}</dd>
    </div>
    <div>
      <dt>Prompt tokens</dt>
      <dd>{count(turn.usage?.prompt_tokens)}</dd>
    </div>
  </dl>
  <p class="caption">
    Full-turn measurements, unchanged by playback speed. Token counts come from
    server usage; decode averages tokens after the first over the recorded
    generation time.
  </p>

  <details class="telemetry">
    <summary>Routing and memory at {seconds(position)}</summary>
    {#if sample && !sample.unavailable}
      <p class="caption">
        Last sample at {seconds(sample.at)}. Service-wide activity, including
        other requests. Hot routing and tier capacities are estimates from the
        live demo.
      </p>
      <dl class="measurements">
        <div>
          <dt>Hot routes</dt>
          <dd>{percent(activity?.hotHits, activity?.totalHits)}</dd>
        </div>
        <div>
          <dt>Warm routes</dt>
          <dd>{percent(activity?.warmHits, activity?.totalHits)}</dd>
        </div>
        <div>
          <dt>Cold routes</dt>
          <dd>{percent(activity?.coldHits, activity?.totalHits)}</dd>
        </div>
        <div>
          <dt>Unclassified</dt>
          <dd>{percent(activity?.unknownHits, activity?.totalHits)}</dd>
        </div>
        <div>
          <dt>Hot capacity</dt>
          <dd>{count(sample.tiers?.hotExperts)} experts</dd>
        </div>
        <div>
          <dt>Warm capacity</dt>
          <dd>{count(sample.tiers?.warmExperts)} experts</dd>
        </div>
        <div>
          <dt>Cold capacity</dt>
          <dd>{count(sample.tiers?.coldExperts)} experts</dd>
        </div>
        <div>
          <dt>KV pages</dt>
          <dd>{count(sample.kvUsedPages)} / {count(sample.kvTotalPages)}</dd>
        </div>
      </dl>
    {:else}
      <p>No telemetry sample available at this point.</p>
    {/if}
  </details>
  {#if turn.finishReason !== "stop"}<p>
      Recording ended with server finish reason: {turn.finishReason}.
    </p>{/if}
  <p class="caption">
    {recording.model}. {recording.hardware}. Build
    <a
      href={`https://github.com/jomcgi-org/freetoken-fork/commit/${recording.build}`}
      >{recording.build.slice(0, 7)}</a
    >. {recording.conditions}
  </p>
</section>

<style>
  .replay {
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  .replay .caption {
    color: var(--ink-2);
    font-size: 0.8rem;
    line-height: 1.6;
  }
  .turns,
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    align-items: center;
    margin: 1rem 0;
  }
  button,
  select {
    font: inherit;
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.5rem 0.7rem;
    border-radius: 3px;
    cursor: pointer;
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
  .conversation {
    border-block: 1px solid var(--stroke);
  }
  summary {
    cursor: pointer;
    line-height: 1.6;
    overflow-wrap: anywhere;
  }
  .prompt {
    padding: 0.8rem 0;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 16rem;
    overflow-y: auto;
    font: inherit;
    font-size: 0.85rem;
  }
  .answer {
    max-height: 24rem;
    min-height: 8rem;
    overflow-y: auto;
    padding: 0.5rem 0;
  }
  .answer p {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    line-height: 1.7;
    margin: 0;
  }
  .controls {
    font-size: 0.85rem;
  }
  .timeline {
    display: flex;
    gap: 0.8rem;
    align-items: center;
    font-size: 0.8rem;
  }
  .timeline input {
    flex: 1;
    min-width: 0;
    accent-color: var(--accent-ink);
  }
  .measurements {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1rem;
    padding: 1rem 0;
    margin-bottom: 0;
  }
  dt {
    font-size: 0.75rem;
    color: var(--ink-2);
  }
  dd {
    margin: 0.3rem 0 0;
    font-family: var(--font-code);
    font-size: 0.9rem;
    overflow-wrap: anywhere;
  }
  .telemetry {
    border-block: 1px solid var(--line);
    padding: 0.75rem 0;
    margin-top: 1rem;
  }
  @media (max-width: 540px) {
    .measurements {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
  }
</style>
