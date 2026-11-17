<script>
  import { decodeSteps } from "./draft-steps.js";

  // The recorded code as it streamed: text a step copied from the input (a
  // verified prompt-lookup draft) is tinted.
  let { events, position, complete, file } = $props();

  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const steps = decodeSteps(events);
  // Each event's step source, to tint the code a lookup step copied.
  const sources = [];
  // svelte-ignore state_referenced_locally (The recording is immutable.)
  const recordedEvents = events;
  {
    let i = 0;
    for (const step of steps) {
      let tokens = 0;
      while (i < recordedEvents.length && tokens < step.tokens) {
        tokens += recordedEvents[i].tokens ?? 1;
        sources.push(step.source);
        i += 1;
      }
    }
  }
  // svelte-ignore state_referenced_locally (A replay is keyed by its recording.)
  const pieces = events.map((event, i) => ({ ...event, source: sources[i] }));
  let pre = $state();
  let arrived = $derived(pieces.filter((piece) => piece.at <= position));
  $effect(() => {
    arrived;
    if (pre) pre.scrollTop = pre.scrollHeight;
  });
</script>

<div class="code-output">
  <div class="code-head">{file}</div>
  <!-- svelte-ignore a11y_no_noninteractive_tabindex (Keyboard users can scroll the output.) -->
  <pre
    bind:this={pre}
    tabindex="0"
    role="region"
    aria-label="Streaming model output"><code
      >{#each arrived as piece}<span class:copied={piece.source === "lookup"}
          >{piece.content}</span
        >{/each}{#if !complete}<span class="stream-cursor" aria-hidden="true"
        ></span>{/if}</code
    ></pre>
</div>

<style>
  .code-output {
    min-width: 0;
    display: grid;
    gap: 0.6rem;
  }
  .code-head {
    color: var(--color-text-accent);
    font: 0.7rem var(--font-family-code);
  }
  pre {
    margin: 0;
    height: 15rem;
    overflow: auto;
    padding: 0.75rem;
    border-left: 3px solid var(--color-text-accent);
    background: var(--color-background-muted);
    color: var(--color-text-primary);
    font: 0.68rem/1.45 var(--font-family-code);
    scrollbar-width: thin;
    scrollbar-color: var(--color-text-accent) var(--color-border);
  }
  .copied {
    background: var(--color-text-accent);
    color: var(--color-background-surface);
    text-decoration: underline;
    text-underline-offset: 0.15em;
  }
  .stream-cursor {
    display: inline-block;
    width: 0;
    height: 1em;
    margin-left: 1px;
    border-left: 2px solid var(--color-text-accent);
    vertical-align: text-bottom;
  }
</style>
