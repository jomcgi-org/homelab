<script>
  import recording from "./qwen-replay.json";

  const turn = recording.turns[0];
  let element;
  let position = $state(0);
  let playing = $state(false);
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
  const uncachedTokens =
    turn.usage.prompt_tokens - (turn.usage.cached_tokens ?? 0);
  const inputText = turn.prompt.replace(/\s+/g, " ");
  const prefillStart =
    turn.progress?.find((event) => event.stage === "prefill")?.at ?? 0;
  // The server supplies start/end only. This scans the input over that interval,
  // without claiming an observed per-token processing position.
  let scanOffset = $derived(
    Math.max(
      0,
      Math.min(
        1,
        (position - prefillStart) / (turn.metrics.ttftMs - prefillStart),
      ),
    ) * Math.max(0, inputText.length - 900),
  );
  let scanStart = $derived(Math.floor(scanOffset / 100) * 100);
  const prefillRate = (uncachedTokens * 1000) / turn.metrics.ttftMs;
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
    if (
      !element ||
      typeof IntersectionObserver === "undefined" ||
      matchMedia("(prefers-reduced-motion: reduce)").matches
    )
      return;
    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          playing = true;
          observer.disconnect();
        }
      },
      { threshold: 0.5 },
    );
    observer.observe(element);
    return () => observer.disconnect();
  });
  $effect(() => {
    if (!playing) return;
    let previous;
    let frame;
    function tick(now) {
      if (previous !== undefined) {
        position = Math.min(turn.durationMs, position + (now - previous));
      }
      previous = now;
      if (position >= turn.durationMs) playing = false;
      else frame = requestAnimationFrame(tick);
    }
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  });
</script>

<section
  bind:this={element}
  class="replay"
  aria-label="Inference on the RTX 4090"
>
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
          <dt
            title="Uncached prompt tokens divided by client time to first token, including request overhead"
          >
            Prefill
          </dt>
          <dd>
            {Math.round(prefillRate).toLocaleString("en-US")}
            <small>tok/s</small>
          </dd>
          <span class="prompt-size"
            >{uncachedTokens.toLocaleString("en-US")} tokens</span
          >
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

    <div class="arrival-strip">
      <svg viewBox="0 0 640 72" role="img" aria-label="Token arrival times">
        <rect
          class="prefill-span"
          x="0"
          y="8"
          width={(turn.events[0].at / turn.durationMs) * 640}
          height="44"
        />
        {#each turn.events as event, index}
          <line
            class:arrived={event.at <= position}
            x1={(event.at / turn.durationMs) * 640}
            x2={(event.at / turn.durationMs) * 640}
            y1={index % 3 === 0 ? 12 : 22}
            y2="52"
          />
        {/each}
        <line
          class="playhead"
          x1={(position / turn.durationMs) * 640}
          x2={(position / turn.durationMs) * 640}
          y1="0"
          y2="60"
        />
        <text x="4" y="70">Prefill</text><text
          x={Math.min(570, (turn.events[0].at / turn.durationMs) * 640 + 4)}
          y="70">Decode</text
        >
      </svg>
    </div>
    <div
      class="input-scan"
      class:finished={phase !== "Prefill"}
      aria-label="Input text scan over the measured prefill interval"
    >
      <div class="scan-heading">
        <span>{uncachedTokens.toLocaleString("en-US")} input tokens</span><span
          >Thinking off</span
        >
      </div>
      <div class="scan-window" aria-hidden="true">
        {#each [0, 1, 2] as row}
          <div class="scan-row">
            <span style={`transform:translateX(-${scanOffset % 100}ch)`}
              >{inputText.slice(
                scanStart + row * 300,
                scanStart + row * 300 + 300,
              )}</span
            >
          </div>
        {/each}
      </div>
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
        <p>
          {answer}{#if position < turn.durationMs}<span
              class="cursor"
              aria-hidden="true"
            ></span>{/if}
        </p>
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
    grid-template-columns: repeat(4, minmax(0, 1fr));
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
  .arrival-strip {
    margin: 1.2rem 0 0.8rem;
  }
  .arrival-strip svg {
    width: 100%;
    display: block;
    overflow: visible;
  }
  .prefill-span {
    fill: color-mix(in srgb, var(--tone-ram) 38%, var(--sheet));
  }
  .arrival-strip line {
    stroke: var(--tone-gpu);
    stroke-width: 2;
    opacity: 0.2;
  }
  .arrival-strip line.arrived {
    opacity: 1;
  }
  .arrival-strip line.playhead {
    stroke: var(--ink);
    opacity: 1;
    stroke-width: 1;
  }
  .arrival-strip text {
    fill: var(--ink-2);
    font: 10px var(--font-code);
  }
  .cursor {
    display: inline-block;
    width: 0.5rem;
    height: 1em;
    margin-left: 0.2rem;
    background: var(--tone-gpu);
    vertical-align: -0.15em;
    animation: blink 1s steps(2) infinite;
  }
  @keyframes blink {
    50% {
      opacity: 0;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cursor {
      animation: none;
    }
  }
  .measurements > div:nth-child(3) dd {
    color: var(--tone-ram);
  }
  .prompt-size {
    display: block;
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
    margin-top: 0.35rem;
  }
  @media (max-width: 600px) {
    .measurements {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .measurements > div:nth-child(3) {
      border-left: 0;
    }
  }
  .input-scan {
    margin: 1rem 0;
    padding: 0.8rem;
    border-left: 3px solid var(--tone-ram);
    background: color-mix(in srgb, var(--tone-ram) 10%, var(--sheet));
  }
  .scan-heading {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    color: var(--tone-ram);
    font: 0.65rem var(--font-code);
    margin-bottom: 0.65rem;
  }
  .scan-window {
    overflow: hidden;
    font: 0.7rem/1.8 var(--font-code);
    color: var(--ink-2);
  }
  .scan-row {
    overflow: hidden;
    white-space: nowrap;
  }
  .scan-row span {
    display: inline-block;
    will-change: transform;
  }
  .input-scan.finished {
    border-color: var(--line);
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
