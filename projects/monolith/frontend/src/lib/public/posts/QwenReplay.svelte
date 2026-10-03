<script>
  import IncidentGraph from "./IncidentGraph.svelte";
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
  const uncachedTokens =
    turn.usage.prompt_tokens - (turn.usage.cached_tokens ?? 0);
  const finalAnswer = turn.events.map((event) => event.content).join("");
  const reportSections = recording.document?.sections ?? [];
  const prefillStart =
    turn.progress?.find((event) => event.stage === "prefill")?.at ?? 0;
  // The server supplies start/end only. This scans the input over that interval,
  // without claiming an observed per-token processing position.
  let scanFraction = $derived(
    Math.max(
      0,
      Math.min(
        1,
        (position - prefillStart) / (turn.metrics.ttftMs - prefillStart),
      ),
    ),
  );
  let pageOffset = $derived(
    scanFraction * Math.max(0, (recording.document?.pages ?? 1) - 4),
  );
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
    Map the controls and failures from the <a
      href={recording.source.url}
      target="_blank"
      rel="noreferrer">OpenAI &lt;&gt; HuggingFace cyber incident postmortem</a
    >.
  </p>
  <div class="demo-body">
    <span class="sr-only" role="status">{phase}</span>
    {#if phase === "Prefill"}
      <div
        class="input-scan"
        aria-label="Report pages across the measured prefill interval"
      >
        <div class="scan-heading">
          <span>{uncachedTokens.toLocaleString("en-US")} input tokens</span
          ><span>Thinking off</span>
        </div>
        <div class="scan-window document-strip" aria-hidden="true">
          <div
            class="document-pages"
            style={`transform:translateX(-${pageOffset * 150}px)`}
          >
            {#each Array.from({ length: recording.document.pages }, (_, i) => i + 1) as page}
              <div class="document-page">
                <span class="page-heading"
                  >{reportSections
                    .filter((section) => section.from <= page)
                    .at(-1)?.label}</span
                >
                <div class="page-lines">
                  {#each Array.from({ length: 12 }, (_, i) => i) as line}<i
                      style={`width:${45 + ((page * 17 + line * 23) % 50)}%`}
                    ></i>{/each}
                </div>
                <span class="page-number">{page}</span>
              </div>
            {/each}
          </div>
        </div>
      </div>
    {:else}
      <div class="answer" role="region" aria-label="Recorded answer">
        <IncidentGraph
          {answer}
          {finalAnswer}
          sourceUrl={recording.source.url}
          review={recording.review}
          complete={phase === "Complete"}
        />
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
    <summary>Prompt</summary>
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
    min-height: 27.5rem;
    display: flex;
    flex-direction: column;
    justify-content: flex-start;
  }
  .input-scan {
    margin-block: auto;
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
  .question a {
    color: inherit;
    text-decoration-color: var(--tone-ram);
    text-underline-offset: 0.2em;
  }
  .document-strip {
    overflow: hidden;
    height: 230px;
  }
  .document-pages {
    display: flex;
    gap: 14px;
    width: max-content;
    will-change: transform;
  }
  .document-page {
    width: 136px;
    height: 218px;
    padding: 14px 12px;
    background: var(--sheet);
    border: 1px solid var(--tone-ram);
    box-sizing: border-box;
    display: flex;
    flex-direction: column;
  }
  .page-heading {
    color: var(--ink);
    font: 0.65rem/1.4 var(--font-code);
    min-height: 3rem;
  }
  .page-lines {
    display: flex;
    flex-direction: column;
    gap: 7px;
    flex: 1;
  }
  .page-lines i {
    display: block;
    height: 2px;
    background: var(--ink-3);
    opacity: 0.5;
  }
  .page-number {
    font: 0.6rem var(--font-code);
    color: var(--ink-2);
    align-self: flex-end;
  }
  .answer {
    font-size: 0.9rem;
    line-height: 1.6;
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
      min-height: 27.5rem;
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
</style>
