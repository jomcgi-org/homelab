<script>
  import { tick } from "svelte";
  import { invalidateAll, replaceState } from "$app/navigation";
  import { SchemeToggle, Seo } from "$lib/public/components";
  import {
    attemptMark,
    attemptWord,
    money,
    nodeWord,
    outcome,
    planStrip,
    plural,
    reviewRounds,
    sessionHref,
    stepsOf,
    stopRows,
    taskSpec,
  } from "$lib/public/factory/activity-view.js";
  import {
    markdownBlocks,
    plainPreview,
  } from "$lib/public/factory/markdown.js";
  import Markdown from "$lib/public/factory/Markdown.svelte";
  import Turn from "$lib/public/factory/Turn.svelte";
  import "$lib/public/factory/factory.css";
  import "$lib/public/factory/activity.css";
  import Trail from "../../../Trail.svelte";

  let { data } = $props();

  let now = $state(data.snapshottedAt ?? new Date().toISOString());
  // 1-based, matching the step numbers on screen and in the #step-N fragment.
  let openStep = $state(null);
  // The plan strip and the step list point at each other through the node
  // key rather than through the DOM, so hovering either one lights both.
  let hotNode = $state(null);

  const REFRESH_MS = 60_000;

  const task = $derived(data.task);
  const policy = $derived(data.policy ?? {});
  const steps = $derived(stepsOf(task));
  const plan = $derived(planStrip(task, steps));
  const runningStep = $derived(
    steps.findIndex((step) => attemptWord(step.attempt.status) === "running"),
  );
  const verdict = $derived(outcome(task, policy, now, runningStep));
  const spec = $derived(taskSpec(task, policy, now));
  const stops = $derived(stopRows(task));
  // The snapshot keeps the first six paragraphs of the issue body, so the
  // brief can end on a heading with nothing under it; that heading is noise.
  const brief = $derived(
    markdownBlocks((task.brief ?? []).join("\n\n")).filter(
      (block, index, all) =>
        !(index === all.length - 1 && block.type === "heading"),
    ),
  );

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
    if (!number || number < 1) return;
    openStep = number;
    writeHash(`#step-${number}`);
    await tick();
    document
      .getElementById(`step-${number}`)
      ?.scrollIntoView({ block: "center" });
  }

  const lastResult = (attempt) => {
    const turns = attempt.turns ?? [];
    return turns.length
      ? plainPreview(turns[turns.length - 1].result_text)
      : "";
  };
</script>

<Seo
  title={`#${task.issue_number} · Factory activity · jomcgi.dev`}
  description={`What the Ember Software Factory did on issue #${task.issue_number}: the plan, every attempt, and the outcome.`}
  path={`/slop/factory/activity/${task.issue_number}`}
/>

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
      <div class="title-block">
        <div class="id">
          <a href={task.url}>#{task.issue_number}</a> · {task.task_class} · gen {task.generation}
        </div>
        <h2>{task.title}</h2>
        <div class="links">
          {#if task.pr}
            <a href={task.pr.url}
              ><span class="mark {verdict.mark}"></span>PR #{task.pr.number} · {task
                .pr.state}</a
            >
          {:else}
            <span>no PR</span>
          {/if}
          <a href={task.url}>issue ›</a>
        </div>

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

    {#if stops.length}
      <section class="panel">
        <p class="sec-label">
          / Stops
          <span class="win"
            >{plural(stops.length, "event")} · {stops.filter(
              (row) => row.person,
            ).length} needed a person</span
          >
        </p>
        <ol class="stops">
          {#each stops as row, index (index)}
            <li class:warn={row.person}>
              <span class="when num">{row.day} {row.clock}</span>
              <span class="act">{row.action}</span>
              <span class="why">{row.reason || "·"}</span>
              <span class="who">{row.person ? "person" : ""}</span>
            </li>
          {/each}
        </ol>
      </section>
    {/if}

    {#if brief.length}
      <section class="panel">
        <p class="sec-label">/ Brief</p>
        <div class="brief md"><Markdown blocks={brief} /></div>
      </section>
    {/if}

    <section class="panel">
      <p class="sec-label">
        / Plan
        <span class="win"
          >{plural(plan.length, "node")} · {plural(
            reviewRounds(task),
            "review round",
          )}</span
        >
      </p>
      {#if plan.length}
        <ol class="plan-strip">
          {#each plan as entry (entry.node.node_key + entry.number)}
            <li
              class="pnode {entry.node.state}"
              class:hot={hotNode === entry.node.node_key}
            >
              <button
                type="button"
                disabled={!entry.step}
                onmouseenter={() => (hotNode = entry.node.node_key)}
                onmouseleave={() => (hotNode = null)}
                onfocus={() => (hotNode = entry.node.node_key)}
                onblur={() => (hotNode = null)}
                onclick={() => openAndScroll(entry.step)}
                aria-label={`${entry.node.node_key}, ${nodeWord(entry.node.state)}${entry.step ? `, open step ${entry.step}` : ""}`}
              >
                <span class="n num">{entry.number}</span>
                <span class="k">{entry.node.node_key}</span>
                <span class="s"
                  ><span
                    class="mark {attemptMark(
                      entry.node.state === 'running'
                        ? 'admitted'
                        : entry.node.state === 'done'
                          ? 'succeeded'
                          : entry.node.state,
                    )}"
                  ></span>{nodeWord(entry.node.state)} · {entry.node
                    .model}{#if entry.attempts > 1}
                    · ×{entry.attempts}{/if}{#if entry.cost}
                    · {money(entry.cost)}{/if}</span
                >
                {#if entry.deps.length}
                  <span class="d">after {entry.deps.join(", ")}</span>
                {/if}
              </button>
            </li>
          {/each}
        </ol>
      {:else}
        <p class="empty">not planned yet</p>
      {/if}
    </section>

    <section class="panel">
      <p class="sec-label">
        / Steps
        <span class="win">{plural(steps.length, "attempt")}</span>
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
                <span class="no num">{number}</span>
                <span class="mark {attemptMark(step.attempt.status)}"></span>
                <span class="main"
                  ><span class="what"
                    >{step.node.node_key}<span class="who"
                      >{step.node.model} · attempt {step.attempt.attempt} of {policy.max_attempts}
                      · {attemptWord(step.attempt.status)}</span
                    ></span
                  ><span class="said">{lastResult(step.attempt)}</span></span
                >
                <span class="cost num"
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
                  <a class="more" href={session}>session record ›</a>
                {/if}
                {#if turns.length}
                  {#each turns as turn, turnIndex (`${turn.seq}-${turnIndex}`)}
                    <Turn {turn} {session} />
                  {/each}
                {:else}
                  <p class="empty">
                    {attemptWord(step.attempt.status) === "running"
                      ? "running · no turn yet"
                      : "no turns recorded"}
                  </p>
                {/if}
              </div>
            </li>
          {/each}
        </ol>
      {:else}
        <p class="empty">no steps yet</p>
      {/if}
    </section>

    <a class="back" href="/slop/factory/activity">← activity</a>
  </div>
</main>
