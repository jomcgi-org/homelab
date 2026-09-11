<script>
  import { invalidateAll } from "$app/navigation";
  import { SchemeToggle, Seo } from "$lib/public/components";
  import {
    duration,
    groupByDay,
    isoClock,
    ledger,
    ledgerMeta,
    money,
    nodeWord,
    relative,
    taskMark,
  } from "$lib/public/factory/activity-view.js";
  import "$lib/public/factory/factory.css";
  import "$lib/public/factory/activity.css";
  import Trail from "../../Trail.svelte";

  let { data } = $props();

  // Relative times need a clock, and the server has a different one from the
  // browser. Starting from the snapshot the payload carries means SSR and the
  // first client render agree; the effect below then tracks real time.
  let now = $state(data.board.snapshotted_at ?? new Date().toISOString());

  const policy = $derived(data.board.policy ?? {});
  const book = $derived(ledger(data.board, now));
  const days = $derived(groupByDay(book.done));

  const REFRESH_MS = 60_000;

  $effect(() => {
    now = new Date().toISOString();
    const timer = setInterval(() => {
      now = new Date().toISOString();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

  // The lane moves while the page is open, so the board refetches itself. Only
  // while the tab is actually being looked at: a backgrounded tab polling the
  // origin for hours is the cost with none of the benefit.
  $effect(() => {
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") invalidateAll();
    }, REFRESH_MS);
    return () => clearInterval(timer);
  });

  const nodeTitle = (task) =>
    (task.nodes ?? [])
      .map((node) => `${node.node_key}: ${nodeWord(node.state)}`)
      .join(", ");

  const startsOf = (task) => (task.state === "queued" ? null : task.turns_used);

  // Each blocks below key on issue number AND position. A keyed each throws at
  // runtime on a duplicate, and an issue re-admitted under a later generation
  // appears twice in the completed ledger, so the number alone is not unique.
  const LEGEND = [
    ["done", "done"],
    ["running", "running"],
    ["queued", "queued"],
    ["failed", "failed"],
    ["uncertain", "uncertain"],
    ["retired", "retired"],
  ];
</script>

<Seo
  title="Factory activity · jomcgi.dev"
  description="Every issue the Ember Software Factory has taken on: what is running now, what landed, and what it cost."
  path="/slop/factory/activity"
/>

{#snippet ledgerHead()}
  <li class="hd">
    <span>Issue</span>
    <span>Task</span>
    <span>Nodes</span>
    <span class="r">Starts</span>
    <span class="r">Spend</span>
    <span class="r">Time</span>
    <span></span>
  </li>
{/snippet}

{#snippet row(task, time)}
  <li>
    <a
      class="row"
      href={`/slop/factory/activity/${task.issue_number}`}
      aria-label={`Open task ${task.issue_number}`}
    >
      <span class="id"
        ><span class="mark {taskMark(task.state)}" title={task.state}
        ></span>#{task.issue_number}</span
      >
      <span
        ><span class="t">{task.title}</span><span class="m"
          >{ledgerMeta(task, policy, now)}</span
        ></span
      >
      <span class="strip-cell">
        {#if task.nodes?.length}
          <span class="strip" title={nodeTitle(task)}
            >{#each task.nodes as node, index (node.node_key + index)}<i
                class={node.state}
              ></i>{/each}</span
          >
        {:else}
          <span class="m">not planned</span>
        {/if}
      </span>
      <span class="r num"
        >{#if startsOf(task) === null}–{:else}{task.turns_used}<span class="of"
            >/{task.allowance_turns}</span
          >{/if}</span
      >
      <span class="r num"
        >{task.state === "queued" ? "–" : money(task.committed_cost_usd)}</span
      >
      <span class="r num">{time}</span>
      <span class="go" aria-hidden="true">›</span>
    </a>
  </li>
{/snippet}

<main class="td factory-page activity-page">
  <div class="frame">
    <header class="masthead">
      <h1 class="sr-only">Ember Software Factory</h1>
      <Trail
        crumbs={[
          { label: "factory", href: "/slop/factory" },
          { label: "activity" },
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

    {#if data.unavailable}
      <p class="unavailable">Unavailable right now.</p>
    {/if}

    <div class="stats">
      <div>
        <div class="k">Line</div>
        <div class="v">
          <span
            class="mark"
            class:running={data.board.state === "enabled"}
            class:queued={data.board.state !== "enabled"}
          ></span>
          {data.unavailable ? "unavailable" : data.board.state}
          {#if policy.generation != null}<small>gen {policy.generation}</small
            >{/if}
        </div>
      </div>
      <div>
        <div class="k">In flight</div>
        <div class="v num">
          {book.live.length}{#if policy.max_tasks != null}<small
              >of {policy.max_tasks}</small
            >{/if}
        </div>
      </div>
      <div>
        <div class="k">Queued</div>
        <div class="v num">{book.queued.length}</div>
      </div>
      <div>
        <div class="k">Landed 7d</div>
        <div class="v num ok">{book.landed}</div>
      </div>
      <div>
        <div class="k">Escalated 7d</div>
        <div class="v num" class:bad={book.escalated > 0}>{book.escalated}</div>
      </div>
      <div>
        <div class="k">Spend 7d</div>
        <div class="v num">{money(book.spend)}</div>
      </div>
    </div>

    <section class="panel">
      <p class="sec-label">
        / In flight
        <span class="win"
          >{data.board.snapshotted_at
            ? `as of ${isoClock(data.board.snapshotted_at)} UTC`
            : "no snapshot yet"} · {book.live.length} running, {book.queued
            .length} queued</span
        >
      </p>
      <ul class="rows">
        {@render ledgerHead()}
        {#each book.live as task, index (`${task.issue_number}-${index}`)}
          {@render row(task, relative(task.admitted_at, now))}
        {/each}
        {#each book.queued as task, index (`${task.issue_number}-${index}`)}
          {@render row(task, "–")}
        {/each}
      </ul>
      {#if !book.live.length && !book.queued.length}
        <p class="none">Nothing is in the lane right now.</p>
      {/if}
      <div class="legend">
        {#each LEGEND as [state, label] (state)}
          <span><span class="mark {state}"></span>{label}</span>
        {/each}
      </div>
    </section>

    <section class="panel">
      <p class="sec-label">
        / Completed
        <span class="win"
          >{book.done.length
            ? `last ${book.done.length}, newest first`
            : "nothing yet"}</span
        >
      </p>
      <ul class="rows">
        {@render ledgerHead()}
        {#each days as day, dayIndex (`${day.day}-${dayIndex}`)}
          <li class="day">{day.day}</li>
          {#each day.tasks as task, index (`${task.issue_number}-${index}`)}
            {@render row(task, duration(task.admitted_at, task.finished_at))}
          {/each}
        {/each}
      </ul>
      {#if !book.done.length}
        <p class="none">Nothing has finished yet.</p>
      {/if}
    </section>

    <p class="foot">
      Each row is one GitHub issue admitted to the lane: planned by the
      conductor, built and reviewed by workers, verified, then its PR enqueued.
      Nothing here merges itself. Open a row for the plan, each step, and the
      transcript behind it.
    </p>
  </div>
</main>
