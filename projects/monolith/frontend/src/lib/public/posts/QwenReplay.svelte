<script>
  import { fade } from "svelte/transition";
  import DemoDisclosure from "./DemoDisclosure.svelte";
  import CodeOutput from "./CodeOutput.svelte";
  import DraftTrace from "./DraftTrace.svelte";
  import { decodeSteps, rollingRate } from "./draft-steps.js";
  import IncidentGraph from "./IncidentGraph.svelte";
  import research from "./qwen-replay.json";
  // `kind` "research" reads a long report and maps it as a graph; "coding" reads
  // a codebase and writes a change, with the per-step draft trace.
  let { landing = false, recording = research, kind = "research" } = $props();
  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const turn = recording.turns[0];
  let element;
  let position = $state(0);
  let playing = $state(false);
  // Landing pages start with the result; the captured text remains available.
  // svelte-ignore state_referenced_locally (This prop selects the initial state.)
  let outputOpen = $state(!landing);
  const fadeDuration = () =>
    typeof Element === "undefined" ||
    typeof Element.prototype.animate !== "function" ||
    (typeof matchMedia !== "undefined" &&
      matchMedia("(prefers-reduced-motion: reduce)").matches)
      ? 0
      : 180;
  let answer = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .map((event) => event.content)
      .join(""),
  );
  const uncachedTokens =
    turn.usage.prompt_tokens - (turn.usage.cached_tokens ?? 0);
  const finalAnswer = turn.events.map((event) => event.content).join("");
  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
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
  // Output rate over the last 1.5 s of decode steps, for the live readout.
  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const rates = rollingRate(decodeSteps(turn.events), 1500);
  let liveRate = $derived(rates.findLast((r) => r.at <= position)?.rate ?? 0);
  let written = $derived(
    turn.events
      .filter((event) => event.at <= position)
      .reduce((n, event) => n + (event.tokens ?? 1), 0),
  );
  let phase = $derived(
    position >= turn.durationMs
      ? "Complete"
      : position < turn.events[0].at
        ? "Prefill"
        : "Decode",
  );
  const seconds = (ms) => (ms == null ? "--" : (ms / 1000).toFixed(1) + " s");
  // The output panel opens while the model writes and closes when it finishes,
  // so the result (the graph) is what remains; it can still be reopened.
  let shownPhase = "Prefill";
  $effect(() => {
    if (phase === shownPhase) return;
    if (phase === "Decode") outputOpen = true;
    if (phase === "Complete") outputOpen = false;
    shownPhase = phase;
  });
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

{#snippet playbackControls()}
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
{/snippet}

<section
  bind:this={element}
  class="replay"
  class:landing
  aria-label="Inference on the RTX 4090"
>
  {#if kind === "coding"}
    <p class="question">
      Read <a href={recording.source.url} target="_blank" rel="noreferrer"
        >the engine's expert-cache crate</a
      > and add a method with a test.
    </p>
  {:else}
    <p class="question">
      Map the controls and failures from the <a
        href={recording.source.url}
        target="_blank"
        rel="noreferrer"
        >OpenAI &lt;&gt; HuggingFace cyber incident postmortem</a
      >.
    </p>
  {/if}
  {#if landing}{@render playbackControls()}{/if}
  <!-- Each measurement fills in as the replay reaches it, in request order. -->
  <dl class="measurements" aria-label="Measured inference performance">
    <div class="prefill-rate" class:live={phase === "Prefill"}>
      <dt
        title="Uncached prompt tokens divided by client time to first token, including request overhead"
      >
        Prefill
      </dt>
      <dd>
        {#if phase === "Prefill"}
          {seconds(Math.max(0, position - prefillStart))}
          <small class="detail"
            >reading {uncachedTokens.toLocaleString("en-US")} tokens</small
          >
        {:else}
          {Math.round(prefillRate).toLocaleString("en-US")} <small>tok/s</small>
        {/if}
      </dd>
    </div>
    <div class="first-token">
      <dt>First token</dt>
      <dd>
        {#if phase === "Prefill"}--{:else}{(turn.metrics.ttftMs / 1000).toFixed(
            1,
          )} <small>s</small>{/if}
      </dd>
    </div>
    <div class="decode-rate" class:live={phase === "Decode"}>
      <dt>Decode</dt>
      <dd>
        {#if phase === "Prefill"}--{:else if phase === "Decode"}{Math.round(
            liveRate,
          )}
          <small>tok/s now</small>
          <small class="detail">{written.toLocaleString("en-US")} tokens</small>
        {:else}{turn.metrics.tokensPerSecond?.toFixed(1) ?? "--"}
          <small>tok/s</small>
          <small class="detail"
            >{turn.usage.completion_tokens.toLocaleString("en-US")} tokens</small
          >
        {/if}
      </dd>
    </div>
  </dl>
  <div class="demo-body" class:output-expanded={outputOpen}>
    <span class="sr-only" role="status">{phase}</span>
    {#if phase === "Prefill"}
      <div
        class="input-scan"
        transition:fade={{ duration: fadeDuration() }}
        aria-label={kind === "coding"
          ? "Source files across the measured prefill interval"
          : "Report pages across the measured prefill interval"}
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
      <div
        class="answer"
        role="region"
        aria-label="Recorded answer"
        transition:fade={{ duration: fadeDuration() }}
      >
        {#if kind === "coding"}
          <CodeOutput
            events={turn.events}
            {position}
            file="src/cache.rs"
            complete={phase === "Complete"}
          />
        {:else}
          <IncidentGraph
            {answer}
            {landing}
            {finalAnswer}
            bind:outputOpen
            sourceUrl={recording.source.url}
            review={recording.review}
            complete={phase === "Complete"}
          />
        {/if}
        <DraftTrace
          events={turn.events}
          {position}
          durationMs={turn.durationMs}
          tinted={kind === "coding"}
          compact={landing}
        />
      </div>
    {/if}
  </div>
  {#if !landing}{@render playbackControls()}{/if}
  <DemoDisclosure class="prompt" label="Prompt">
    <pre>{turn.prompt}</pre>
  </DemoDisclosure>
</section>

<style>
  .replay {
    min-width: 0;
    color: var(--ink);
    font-family: var(--font-ui);
  }
  .landing {
    container-type: inline-size;
  }
  .landing .question {
    margin-bottom: 0.75rem;
    font-size: 1rem;
  }
  /* Reserve the final graph: 415 / 840 high, using 1.8 / 2.8 of the width,
     but no taller than the viewport leaves (IncidentGraph caps the graph to
     the same 100svh - 35rem budget; the compact trace below takes about 6rem). */
  .landing .demo-body,
  .landing .demo-body.output-expanded {
    min-height: max(
      15.5rem,
      min(calc((100cqw - 1.5rem) * 0.3176), calc(100svh - 35rem))
    );
  }
  /* The code view gives up height before the page does: the chrome above,
     the trace and the link below take about 36.5rem. */
  .landing :global(.code-output pre) {
    height: clamp(8rem, calc(100svh - 36.5rem), 15rem);
  }
  .landing .measurements {
    margin-block: 0.25rem 0.75rem;
    padding-block: 0.6rem;
  }
  .landing .controls {
    padding-block: 0;
  }
  .landing .controls button,
  .landing .timeline {
    min-height: 2.75rem;
    display: flex;
    align-items: center;
  }
  .question {
    margin: 0.2rem 0 1.2rem;
    font-size: 1.2rem;
    font-weight: 500;
    line-height: 1.4;
  }
  .demo-body {
    min-height: 27.5rem;
    display: grid;
    transition: min-height 180ms ease;
  }
  .demo-body.output-expanded {
    min-height: 33.5rem;
  }
  .answer,
  .input-scan {
    min-width: 0;
    grid-area: 1 / 1;
  }
  .answer {
    align-self: start;
  }
  .input-scan {
    align-self: center;
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
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 1rem;
    margin: 0 0 0.8rem;
    padding-block: 0.8rem;
    border-block: 1px solid var(--line);
  }
  /* Detail (tokens read or written) sits under the number, so the number and
     its unit never wrap apart. */
  .measurements .detail {
    display: block;
    margin-top: 0.2rem;
  }
  .measurements .live dt::after {
    content: " ●";
    color: var(--tone-hot);
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
  pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    max-height: 12rem;
    overflow-y: auto;
    font: 0.75rem/1.5 var(--font-code);
  }
  button:focus-visible,
  input:focus-visible {
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
  @media (prefers-reduced-motion: reduce) {
    .demo-body {
      transition: none;
    }
  }
  @media (max-width: 900px) {
    .landing .demo-body,
    .landing .demo-body.output-expanded {
      min-height: max(28.25rem, calc(100cqw * 0.49405 + 12rem));
    }
  }
  /* Short laptop screens get the phone's compact page strip, and the code
     view may shrink further, so the landing still fits one screen. */
  @media (max-height: 820px) {
    .landing .document-strip {
      height: 140px;
    }
    .landing .document-page {
      height: 128px;
    }
    .landing .page-lines i:nth-child(n + 5) {
      display: none;
    }
    .landing :global(.code-output pre) {
      height: clamp(5.5rem, calc(100svh - 36.5rem), 15rem);
    }
  }
  @media (max-width: 600px) {
    /* Reserve only the visible scan on phones. A fixed desktop-sized graph
       placeholder pushed both the demo and its controls below the fold. */
    .landing .demo-body,
    .landing .demo-body.output-expanded {
      min-height: 0;
    }
    .landing .input-scan {
      padding: 0.65rem;
    }
    .landing .document-strip {
      height: 140px;
    }
    .landing .document-page {
      height: 128px;
    }
    .landing .page-lines i:nth-child(n + 5) {
      display: none;
    }
    .landing .question {
      font-size: 0.9rem;
      margin-bottom: 0.25rem;
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
