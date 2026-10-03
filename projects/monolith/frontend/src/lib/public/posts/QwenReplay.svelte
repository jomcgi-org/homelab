<script>
  import GeneratedDiagram from "./GeneratedDiagram.svelte";
  import { splitGeneratedAnswer } from "./generated-answer.js";
  import recording from "./qwen-replay.json";

  const turn = recording.turns[0];
  const finalDiagram = splitGeneratedAnswer(
    turn.events.map((event) => event.content).join(""),
  ).renderable;
  let element;
  let position = $state(0);
  let playing = $state(false);
  let answer = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .map((event) => event.content)
      .join(""),
  );
  let generated = $derived(splitGeneratedAnswer(answer));
  const uncachedTokens =
    turn.usage.prompt_tokens - (turn.usage.cached_tokens ?? 0);
  const inputText = turn.prompt
    .replace(/^Request [^\n]+\n/, "")
    .replace(/\s+/g, " ");
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
  function toggle() {
    if (position >= turn.durationMs) position = 0;
    playing = !playing;
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
          void import("mermaid");
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
  <p class="question">
    How did the Apollo 13 crew get home? Explain it, then draw it.
  </p>
  <div class="demo-body">
    <span class="sr-only" role="status">{phase}</span>
    {#if phase === "Prefill"}
      <div
        class="input-scan"
        aria-label="Input text scan over the measured prefill interval"
      >
        <div class="scan-heading">
          <span>{uncachedTokens.toLocaleString("en-US")} input tokens</span
          ><span>Thinking off</span>
        </div>
        <div class="scan-window" aria-hidden="true">
          {#each [0, 1, 2, 3, 4, 5] as row}
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
    {:else}
      <div class="answer" role="region" aria-label="Recorded answer">
        <p>
          {generated.prose}{#if position < turn.durationMs && !generated.code}<span
              class="cursor"
              aria-hidden="true"
            ></span>{/if}
        </p>
        {#if generated.code}<GeneratedDiagram
            code={generated.code}
            source={generated.renderable}
            finalSource={finalDiagram}
            complete={phase === "Complete"}
          />{/if}
      </div>
    {/if}
  </div>
  <dl class="measurements" aria-label="Measured inference performance">
    <div class="first-token">
      <dt>First token</dt>
      <dd>{(turn.metrics.ttftMs / 1000).toFixed(1)} <small>s</small></dd>
    </div>
    <div class="decode-rate">
      <dt>Decode</dt>
      <dd>
        {turn.metrics.tokensPerSecond?.toFixed(1) ?? "--"} <small>tok/s</small>
      </dd>
    </div>
    <div class="prefill-rate">
      <dt
        title="Uncached prompt tokens divided by client time to first token, including request overhead"
      >
        Prefill
      </dt>
      <dd>
        {Math.round(prefillRate).toLocaleString("en-US")} <small>tok/s</small>
      </dd>
    </div>
  </dl>
  <div class="controls">
    <button type="button" onclick={toggle}
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
  <details class="prompt">
    <summary>Full prompt</summary>
    <pre>{turn.prompt}</pre>
  </details>
</section>

<style>
  .replay {
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  .question {
    margin: 0.2rem 0 1.2rem;
    font-size: 1.2rem;
    font-weight: 500;
    line-height: 1.4;
  }
  .demo-body {
    min-height: 17rem;
  }
  .input-scan {
    padding: 1rem;
    border-left: 3px solid var(--tone-ram);
    background: color-mix(in srgb, var(--tone-ram) 9%, var(--sheet));
  }
  .scan-heading {
    display: flex;
    justify-content: space-between;
    gap: 0.5rem;
    color: var(--tone-ram);
    font: 0.7rem var(--font-code);
    margin-bottom: 0.9rem;
  }
  .scan-window {
    overflow: hidden;
    font: 0.75rem/2 var(--font-code);
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
  .answer {
    font-size: 0.9rem;
    line-height: 1.6;
  }
  .answer > p {
    margin: 0;
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
  .measurements {
    display: flex;
    justify-content: space-between;
    gap: 1rem;
    margin: 0.8rem 0 0;
    padding-block: 0.8rem;
    border-top: 1px solid var(--line);
  }
  .prefill-rate {
    order: 1;
  }
  .decode-rate {
    order: 2;
  }
  .first-token {
    order: 3;
  }
  dt {
    font: 0.65rem var(--font-code);
    color: var(--ink-2);
  }
  dd {
    margin: 0.35rem 0 0;
    font: 1.3rem var(--font-code);
    color: var(--tone-gpu);
  }
  .prefill-rate dd {
    color: var(--tone-ram);
  }
  dd small {
    color: var(--ink-2);
    font-size: 0.65rem;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 0.7rem;
    padding-block: 0.5rem;
  }
  .controls button {
    padding: 0.25rem 0;
    min-width: 3.5rem;
    color: var(--ink-2);
    background: transparent;
    border: 0;
    font: 0.7rem var(--font-code);
    cursor: pointer;
    text-align: left;
  }
  .timeline {
    flex: 1;
    min-width: 0;
  }
  input {
    display: block;
    width: 100%;
    height: 3px;
    accent-color: var(--tone-gpu);
  }
  .time {
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
  }
  .prompt {
    margin-top: 0.4rem;
  }
  summary {
    color: var(--ink-2);
    font: 0.65rem var(--font-code);
    cursor: pointer;
  }
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 12rem;
    overflow-y: auto;
    font: 0.75rem/1.5 var(--font-code);
  }
  button:focus-visible,
  input:focus-visible,
  summary:focus-visible {
    outline: 2px solid var(--accent-ink);
    outline-offset: 3px;
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
  @keyframes blink {
    50% {
      opacity: 0;
    }
  }
  @media (max-width: 600px) {
    .demo-body {
      min-height: 15rem;
    }
    .question {
      font-size: 1.05rem;
    }
    dd {
      font-size: 1.1rem;
    }
    .measurements {
      gap: 0.5rem;
    }
  }
  @media (prefers-reduced-motion: reduce) {
    .cursor {
      animation: none;
    }
  }
</style>
