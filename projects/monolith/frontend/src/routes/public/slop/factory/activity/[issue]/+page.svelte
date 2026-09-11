<script>
  import { tick } from "svelte";
  import { invalidateAll, replaceState } from "$app/navigation";
  import { SchemeToggle, Seo } from "$lib/public/components";
  import {
    activitySummary,
    attemptMark,
    attemptWord,
    briefRuns,
    clip,
    commitUrl,
    isoClock,
    isoDay,
    money,
    nodeWord,
    outcome,
    planLayout,
    plural,
    reviewRounds,
    sessionHref,
    stepsOf,
    taskSpec,
  } from "$lib/public/factory/activity-view.js";
  import "$lib/public/factory/factory.css";
  import "$lib/public/factory/activity.css";
  import Trail from "../../../Trail.svelte";

  let { data } = $props();

  let now = $state(data.snapshottedAt ?? new Date().toISOString());
  // 1-based, matching the step numbers on screen and in the #step-N fragment.
  let openStep = $state(null);
  // The figure and the step list point at each other through the node key
  // rather than through the DOM, so hovering either one lights both.
  let hotNode = $state(null);
  // One record of which long prompts and replies have been opened, keyed by
  // turn. Collapsed is the default: a conductor brief is thousands of
  // characters and would otherwise bury every reply under it.
  let openText = $state({});

  const toggleText = (key) => (openText[key] = !openText[key]);

  const REFRESH_MS = 60_000;

  const task = $derived(data.task);
  const policy = $derived(data.policy ?? {});
  const steps = $derived(stepsOf(task));
  const layout = $derived(planLayout(task.nodes ?? [], steps));
  const runningStep = $derived(
    steps.findIndex((step) => attemptWord(step.attempt.status) === "running"),
  );
  const verdict = $derived(outcome(task, policy, now, runningStep));
  const spec = $derived(taskSpec(task, policy, now));
  const rounds = $derived(reviewRounds(task));

  function stepFromHash(hash) {
    const match = /^#step-(\d+)$/.exec(hash ?? "");
    return match ? Number(match[1]) : null;
  }

  // The fragment is the page's only piece of URL state, and it is a fragment
  // rather than a query so it is a plain anchor target: reading it on mount and
  // on hashchange keeps a deep link, a back button and a click in agreement
  // without a navigation on every disclosure.
  $effect(() => {
    const apply = () => {
      const step = stepFromHash(location.hash);
      if (step) openStep = step;
    };
    apply();
    addEventListener("hashchange", apply);
    return () => removeEventListener("hashchange", apply);
  });

  $effect(() => {
    now = new Date().toISOString();
    const timer = setInterval(() => {
      now = new Date().toISOString();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

  $effect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") invalidateAll();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

  // SvelteKit's replaceState rather than the browser's: the native one leaves
  // page.url pointing at the old address, and it is the router, not this
  // component, that owns what the address bar says. Replace, never push: the
  // back button should leave the page, not walk back through the steps opened
  // on the way down it.
  function writeHash(fragment) {
    replaceState(
      fragment ? `${location.pathname}${fragment}` : location.pathname,
      {},
    );
  }

  function toggleStep(number) {
    openStep = openStep === number ? null : number;
    writeHash(openStep ? `#step-${openStep}` : "");
  }

  // Await the flush before scrolling: the row exists either way, but centring
  // it while its transcript is still collapsed lands the viewport on the wrong
  // place and the expansion then pushes the content out from under the reader.
  async function openAndScroll(number) {
    if (number < 1) return;
    openStep = number;
    writeHash(`#step-${number}`);
    await tick();
    document
      .getElementById(`step-${number}`)
      ?.scrollIntoView({ block: "center" });
  }

  function nodeKeyDown(event, number) {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    openAndScroll(number);
  }

  const lastResult = (attempt) => {
    const turns = attempt.turns ?? [];
    return turns.length ? turns[turns.length - 1].result_text : "no turns yet";
  };
</script>

<Seo
  title={`#${task.issue_number} · Factory activity · jomcgi.dev`}
  description={`What the Ember Software Factory did on issue #${task.issue_number}: the plan, every attempt, and the outcome.`}
  path={`/slop/factory/activity/${task.issue_number}`}
/>

{#snippet long(text, key, cls)}
  {@const cut = clip(text ?? "")}
  <p class={cls || undefined}>
    {cut.clipped && !openText[key]
      ? cut.head
      : (text ?? "")}{#if cut.clipped}<button
        class="more-tog"
        type="button"
        aria-expanded={Boolean(openText[key])}
        onclick={() => toggleText(key)}
        >{openText[key]
          ? "hide −"
          : `show all (${(text ?? "").length} chars) +`}</button
      >{/if}
  </p>
{/snippet}

<main class="td factory-page activity-page">
  <div class="frame">
    <header class="masthead">
      <h1 class="sr-only">Ember Software Factory</h1>
      <Trail
        crumbs={[
          { label: "factory", href: "/slop/factory" },
          { label: "activity", href: "/slop/factory/activity" },
          { label: `#${task.issue_number}` },
        ]}
      />
      <div class="mast-actions">
        <nav class="view-tabs" aria-label="Factory views">
          <a href="/slop/factory">overview</a>
          <a class="here" href="/slop/factory/activity" aria-current="page"
            >activity</a
          >
          <a href="/slop/factory/context">context</a>
        </nav>
        <SchemeToggle />
      </div>
    </header>

    <div class="task-head">
      <div>
        <div class="id">
          task · issue <a href={task.url}>#{task.issue_number}</a> ·
          {task.task_class} · generation {task.generation}
        </div>
        <h2>{task.title}</h2>
        <div class="links">
          {#if task.pr}
            <a href={task.pr.url}>PR #{task.pr.number} · {task.pr.state}</a>
          {:else}
            <span>no PR yet</span>
          {/if}
          <a href={task.url}>issue on GitHub</a>
        </div>
        {#if task.brief?.length}
          <div class="brief">
            {#each task.brief as paragraph, index (index)}
              {@const runs = briefRuns(paragraph)}
              {#if runs.length === 1 && runs[0].heading}
                <p class="h">{runs[0].text}</p>
              {:else}
                <p>
                  {#each runs as run, runIndex (runIndex)}{#if run.code}<span
                        class="code">{run.text}</span
                      >{:else if run.strong}<b>{run.text}</b
                      >{:else}{run.text}{/if}{/each}
                </p>
              {/if}
            {/each}
          </div>
        {/if}

        <section class="panel outcome-panel">
          <p class="sec-label">/ Outcome</p>
          <div class="verdict {verdict.tone}">
            <span class="big"
              ><span class="mark {verdict.mark}"></span>{verdict.headline}</span
            >
            <p>
              {#each verdict.parts as part, index (index)}{#if part.href}<a
                    href={part.href}>{part.text}</a
                  >{:else if part.code}<span class="code">{part.text}</span
                  >{:else if part.step}<a
                    href={`#step-${part.step}`}
                    onclick={(event) => {
                      event.preventDefault();
                      openAndScroll(part.step);
                    }}>{part.text}</a
                  >{:else}{part.text}{/if}{/each}
            </p>
          </div>
          {#each task.stop_events ?? [] as event, index (index)}
            <div class="notice" class:warn={event.intervention_required}>
              <b>{event.intervention_required ? "needs a person" : "note"}</b>
              <span
                >{event.reason}<span class="when"
                  >{event.action} · {isoDay(event.at)}
                  {isoClock(event.at)}</span
                ></span
              >
            </div>
          {/each}
        </section>
      </div>

      <div class="spec">
        {#each spec as field (field.label)}
          <div>
            <span>{field.label}</span>
            <span class:num={field.num}>
              {#if field.mark}
                <span class="state"
                  ><span class="mark {field.mark}"></span>{field.value}</span
                >
              {:else}
                {field.value}
              {/if}
              {#if field.note}<span class="note">{field.note}</span>{/if}
            </span>
          </div>
        {/each}
      </div>
    </div>

    <section class="panel">
      <p class="sec-label">
        / Plan
        <span class="win"
          >conductor {policy.conductor_model} · click a node to open its step</span
        >
      </p>
      {#if layout.nodes.length}
        <div class="fig">
          <svg
            viewBox={`0 0 ${layout.width} ${layout.height}`}
            width={layout.width}
            height={layout.height}
            role="group"
            aria-label={`Plan: ${plural(layout.nodes.length, "node")} in ${plural(layout.stages, "stage")}`}
          >
            <defs>
              <pattern
                id="plan-hatch"
                width="4"
                height="4"
                patternUnits="userSpaceOnUse"
                patternTransform="rotate(45)"
              >
                <rect width="4" height="4" fill="var(--sheet)" />
                <rect width="1" height="4" fill="var(--ink-3)" />
              </pattern>
            </defs>
            {#each layout.edges as edge, index (index)}
              <path class="edge" class:dead={edge.dead} d={edge.d} />
              <path class="arrow" d={edge.arrow} />
            {/each}
            {#each layout.nodes as box (box.index)}
              <g
                class="node {box.node.state}"
                class:hot={hotNode === box.node.node_key}
                tabindex="0"
                role="button"
                aria-label={`${box.node.node_key}, ${nodeWord(box.node.state)}${box.step >= 0 ? `, open step ${box.step + 1}` : ""}`}
                onmouseenter={() => (hotNode = box.node.node_key)}
                onmouseleave={() => (hotNode = null)}
                onfocus={() => (hotNode = box.node.node_key)}
                onblur={() => (hotNode = null)}
                onclick={() => openAndScroll(box.step + 1)}
                onkeydown={(event) => nodeKeyDown(event, box.step + 1)}
              >
                <title>{box.node.node_key}</title>
                <rect
                  class="box"
                  x={box.x}
                  y={box.y}
                  width={box.width}
                  height={box.height}
                />
                <text class="l" x={box.x + 10} y={box.y + 18}>{box.label}</text>
                <text class="s" x={box.x + 10} y={box.y + 33}
                  >{box.node.model} · {nodeWord(box.node.state)}</text
                >
                <g class="n">
                  <circle cx={box.x} cy={box.y} r="8" />
                  <text x={box.x} y={box.y + 3.5} text-anchor="middle"
                    >{box.number}</text
                  >
                </g>
              </g>
            {/each}
          </svg>
        </div>
        <p class="cap">
          <span><b>Fig 1</b> · the plan as applied, left to right</span>
          <span
            >{plural(layout.nodes.length, "node")} · {plural(
              rounds,
              "review round",
            )} added by the engine</span
          >
        </p>
      {:else}
        <p class="empty">Not planned yet.</p>
      {/if}
    </section>

    <section class="panel">
      <p class="sec-label">
        / Steps
        <span class="win"
          >one per attempt, in run order · open a step for its turns: the
          instruction in grey, what the worker ran, its reply in black</span
        >
      </p>
      {#if steps.length}
        <ol class="steps">
          {#each steps as step, index (`${step.node.node_key}-${step.attempt.attempt}-${index}`)}
            {@const number = index + 1}
            {@const open = openStep === number}
            {@const turns = step.attempt.turns ?? []}
            {@const session = step.attempt.session_key
              ? sessionHref(
                  task.issue_number,
                  step.node.node_key,
                  step.attempt.attempt,
                )
              : null}
            <li
              class:open
              class:hot={hotNode === step.node.node_key}
              id={`step-${number}`}
              onmouseenter={() => (hotNode = step.node.node_key)}
              onmouseleave={() => (hotNode = null)}
              onfocusin={() => (hotNode = step.node.node_key)}
              onfocusout={() => (hotNode = null)}
            >
              <button
                class="step-btn"
                type="button"
                aria-expanded={open}
                aria-controls={`transcript-${number}`}
                onclick={() => toggleStep(number)}
              >
                <span class="no">{number}</span>
                <span class="mark {attemptMark(step.attempt.status)}"></span>
                <span
                  ><span class="what"
                    >{step.node.node_key}<span class="who"
                      >{step.node.model} · attempt {step.attempt.attempt} of {policy.max_attempts}
                      ·
                      {attemptWord(step.attempt.status)}</span
                    ></span
                  ><span class="said">{lastResult(step.attempt)}</span></span
                >
                <span class="cost"
                  >{plural(turns.length, "turn")} · {money(
                    step.attempt.cost_usd,
                  )}</span
                >
                <span class="tog" aria-hidden="true">{open ? "−" : "+"}</span>
              </button>
              <div
                class="transcript"
                id={`transcript-${number}`}
                hidden={!open}
              >
                {#if session}
                  <a class="more" href={session}
                    >full session: every turn, each edit's hunk, the patch ›</a
                  >
                {/if}
                {#if turns.length}
                  {#each turns as turn, turnIndex (`${turn.seq}-${turnIndex}`)}
                    <div class="turn">
                      <span class="tn">{turn.seq}</span>
                      {@render long(
                        turn.prompt,
                        `ask-${number}-${turn.seq}`,
                        "ask",
                      )}
                      {#if turn.activities?.length}
                        {@const digest = activitySummary(turn.activities)}
                        <div class="digest">
                          <p class="did">
                            {#each digest.counts as count (count.kind)}
                              <span>{plural(count.count, count.kind)}</span>
                            {/each}
                          </p>
                          <ul class="did-rows">
                            {#each digest.shown as row, rowIndex (rowIndex)}
                              <li title={row.title}>
                                <span class="ty">{row.type}</span>
                                <span class="what">{row.text}</span>
                              </li>
                            {/each}
                          </ul>
                          {#if digest.hidden > 0}
                            {#if session}
                              <a class="more" href={session}
                                >and {digest.hidden} more in the session ›</a
                              >
                            {:else}
                              <!-- A span, not a paragraph: `.turn p` would win
                                   the font size back off `.more`. -->
                              <span class="more">and {digest.hidden} more</span>
                            {/if}
                          {/if}
                        </div>
                      {/if}
                      {@render long(
                        turn.result_text,
                        `say-${number}-${turn.seq}`,
                        "",
                      )}
                      <p class="meta">
                        {money(turn.cost_usd)}{#if turn.commit_sha}
                          · commit <a
                            class="sha"
                            href={commitUrl(turn.commit_sha)}
                            >{turn.commit_sha}</a
                          >{/if}
                      </p>
                    </div>
                  {/each}
                {:else}
                  <div class="turn">
                    <span class="tn">·</span>
                    <p class="ask">
                      {attemptWord(step.attempt.status) === "running"
                        ? "Running. The first turn has not returned yet."
                        : "No turns were recorded for this attempt."}
                    </p>
                  </div>
                {/if}
              </div>
            </li>
          {/each}
        </ol>
      {:else}
        <p class="empty">
          No steps yet. The conductor plans once a slot opens.
        </p>
      {/if}
    </section>

    <a class="back" href="/slop/factory/activity">← activity</a>
  </div>
</main>
