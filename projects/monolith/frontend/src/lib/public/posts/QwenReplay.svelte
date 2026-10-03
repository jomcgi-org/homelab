<script>
  import recording from "./qwen-replay.json";

  const turn = recording.turns[0];
  let position = $state(0);
  let playing = $state(false);
  let speed = $state(1);
  let answer = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .map((event) => event.content)
      .join(""),
  );
  let statsSample = $derived(
    turn.statsSamples.findLast((item) => item.at <= position) ??
      turn.statsSamples[0],
  );
  let stats = $derived(statsSample?.unavailable ? null : statsSample);
  let phase = $derived(
    position >= turn.durationMs
      ? "Complete"
      : position < turn.events[0].at
        ? "Prefill"
        : "Decode",
  );
  const seconds = (ms) => (ms == null ? "--" : (ms / 1000).toFixed(1) + " s");
  const gb = (n) => (n == null ? "--" : (n / 1e9).toFixed(1));
  function toggle() {
    if (position >= turn.durationMs) position = 0;
    playing = !playing;
  }
  function seek(at) {
    playing = false;
    position = at;
  }
  $effect(() => {
    if (!playing) return;
    const rate = speed;
    let previous;
    let frame;
    function tick(now) {
      if (previous !== undefined) {
        position = Math.min(
          turn.durationMs,
          position + (now - previous) * rate,
        );
      }
      previous = now;
      if (position >= turn.durationMs) playing = false;
      else frame = requestAnimationFrame(tick);
    }
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  });
</script>

<section class="replay" aria-label="Inference on the RTX 4090">
  <div class="transport-heading">
    <button
      type="button"
      class="speed-control"
      aria-label={"Playback speed " + speed + " times. Click to change."}
      onclick={() => (speed = speed === 8 ? 1 : speed * 2)}
      >{speed}× speed</button
    >
  </div>
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
  <nav class="phase-navigation" aria-label="Jump to request phase">
    <button
      type="button"
      aria-pressed={phase === "Prefill"}
      onclick={() => seek(0)}>Prefill</button
    >
    <button
      type="button"
      aria-pressed={phase === "Decode"}
      onclick={() => seek(turn.events[0].at)}>First token</button
    >
    <button
      type="button"
      aria-pressed={phase === "Complete"}
      onclick={() => seek(turn.durationMs)}>Complete</button
    >
  </nav>
  <div class="replay-stage">
    <div class="instrument">
      <span class="sr-only" role="status">{phase}</span>
      <dl
        class="measurements"
        aria-label="Recorded service throughput and memory"
      >
        <div>
          <dt>First token</dt>
          <dd>
            {(turn.metrics.ttftMs / 1000).toFixed(1)}
            <small>s</small>
          </dd>
        </div>
        <div>
          <dt>Decode</dt>
          <dd>
            {turn.metrics.tokensPerSecond?.toFixed(1) ?? "--"}
            <small>tok/s</small>
          </dd>
        </div>
        <div>
          <dt>GPU memory</dt>
          <dd>
            {gb(stats?.vramBytes)}
            <small>GB</small>
          </dd>
        </div>
      </dl>
    </div>

    <div class="conversation">
      <details class="prompt">
        <summary>Prompt</summary>
        <pre>{turn.prompt}</pre>
      </details>
      <!-- Keyboard users need to focus this region to scroll a long answer. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div
        class="answer"
        tabindex="0"
        role="region"
        aria-label="Recorded answer"
      >
        <p>{answer || "Waiting for the first token..."}</p>
      </div>
      {#if turn.note && position >= turn.durationMs}<p class="caption">
          {turn.note}
        </p>{/if}
    </div>
  </div>
</section>

<style>
  .replay {
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  button {
    font: 0.75rem var(--font-code);
    color: var(--ink);
    background: var(--sheet);
    border: 1px solid var(--stroke);
    padding: 0.5rem 0.65rem;
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
  .transport-heading {
    display: flex;
    justify-content: flex-end;
  }
  .controls {
    display: flex;
    gap: 0.6rem;
    align-items: center;
    margin: 0.8rem 0;
  }
  .controls button {
    width: 5rem;
    flex-shrink: 0;
  }
  .controls :global(button[data-pointer]:focus) {
    outline: none;
  }
  .timeline {
    flex: 1;
    min-width: 3rem;
  }
  .timeline input {
    width: 100%;
    accent-color: var(--tone-gpu);
  }
  .time {
    min-width: 4rem;
    text-align: right;
    font: 0.75rem var(--font-code);
  }
  .phase-navigation {
    display: flex;
    gap: 0.4rem;
    margin-bottom: 1rem;
  }
  .phase-navigation button {
    flex: 1;
    border: 0;
    border-bottom: 2px solid var(--line);
  }
  .phase-navigation button[aria-pressed="true"] {
    border-color: var(--tone-gpu);
    background: color-mix(in srgb, var(--tone-gpu) 9%, var(--sheet));
  }
  .measurements {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    margin: 0;
    border-block: 1px solid var(--line);
  }
  .measurements > div {
    padding: 1rem 0.6rem;
  }
  .measurements > div + div {
    border-left: 1px solid var(--line);
  }
  dt {
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
  }
  dd {
    margin: 0.5rem 0 0;
    font: 1.65rem var(--font-code);
    color: var(--tone-gpu);
  }
  dd small {
    font-size: 0.7rem;
    color: var(--ink-2);
  }
  .prompt {
    padding-block: 0.75rem;
  }
  summary {
    cursor: pointer;
    font: 0.7rem var(--font-code);
    color: var(--ink-2);
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 12rem;
    overflow-y: auto;
    font: 0.8rem/1.5 var(--font-ui);
  }
  .answer {
    min-height: 6rem;
    padding-top: 0.5rem;
    font-size: 0.9rem;
    line-height: 1.7;
  }
  .answer p {
    margin: 0;
    white-space: pre-wrap;
  }
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
  .caption {
    font-size: 0.75rem;
  }
</style>
