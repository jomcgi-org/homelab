<script>
  import { invalidateAll, replaceState } from "$app/navigation";
  import { page } from "$app/state";
  import { SchemeToggle, Seo } from "$lib/public/components";
  import {
    duration,
    filterQuery,
    filterTasks,
    groupByDay,
    isoClock,
    LEDGER_PAGE,
    LEDGER_SORTS,
    LEDGER_STATES,
    ledger,
    ledgerMeta,
    money,
    nodeWord,
    readFilters,
    relative,
    taskMark,
    taskTypes,
  } from "$lib/public/factory/activity-view.js";
  import { paginate } from "$lib/public/factory/model.js";
  import "$lib/public/factory/factory.css";
  import "$lib/public/factory/activity.css";
  import Trail from "../../Trail.svelte";

  let { data } = $props();

  // Relative times need a clock, and the server has a different one from the
  // browser. Starting from the snapshot the payload carries means SSR and the
  // first client render agree; the effect below then tracks real time.
  let now = $state(data.board.snapshotted_at ?? new Date().toISOString());

  // The filters are the page's URL state, read once from the address on both
  // the server and the client so a shared link renders filtered. Each change
  // is written back with replaceState, never pushed: the back button should
  // leave the page, not walk back through every keystroke.
  let filters = $state(readFilters(page.url.searchParams));

  const policy = $derived(data.board.policy ?? {});
  const book = $derived(ledger(data.board, now));
  const everything = $derived([...book.live, ...book.queued, ...book.done]);
  const types = $derived(taskTypes(everything));
  const live = $derived(filterTasks([...book.live, ...book.queued], filters));
  const done = $derived(filterTasks(book.done, filters));
  const pages = $derived(paginate(done, filters.page - 1, LEDGER_PAGE));
  const days = $derived(groupByDay(pages.rows));
  const filtering = $derived(
    Boolean(filters.q || filters.state || filters.type),
  );

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

  function set(patch) {
    filters = { ...filters, page: 1, ...patch };
    replaceState(`${location.pathname}${filterQuery(filters)}`, {});
  }

  const nodeTitle = (task) =>
    (task.nodes ?? [])
      .map((node) => `${node.node_key}: ${nodeWord(node.state)}`)
      .join(", ");

  const startsOf = (task) => (task.state === "queued" ? null : task.turns_used);

  const rowMeta = (task) =>
    [task.task_class, ledgerMeta(task, policy, now)]
      .filter(Boolean)
      .join(" · ");
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
          >{rowMeta(task)}</span
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
        >{task.state === "queued" ? "–" : money(task.cost_usd)}</span
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

    <div class="filters" role="search" aria-label="Filter the ledger">
      <input
        type="search"
        placeholder="issue, title, phase"
        aria-label="Search tasks"
        autocomplete="off"
        value={filters.q}
        oninput={(event) => set({ q: event.currentTarget.value })}
      />
      <div class="chips" role="group" aria-label="State">
        {#each ["", ...LEDGER_STATES] as state (state)}
          <button
            type="button"
            class:on={filters.state === state}
            aria-pressed={filters.state === state}
            onclick={() => set({ state })}
            >{#if state}<span class="mark {taskMark(state)}"
              ></span>{/if}{state || "all"}</button
          >
        {/each}
      </div>
      <label
        ><span class="k">type</span><select
          value={filters.type}
          onchange={(event) => set({ type: event.currentTarget.value })}
        >
          <option value="">all</option>
          {#each types as type (type)}
            <option value={type}>{type}</option>
          {/each}
        </select></label
      >
      <label
        ><span class="k">sort</span><select
          value={filters.sort}
          onchange={(event) => set({ sort: event.currentTarget.value })}
        >
          {#each LEDGER_SORTS as sort (sort)}
            <option value={sort}>{sort}</option>
          {/each}
        </select></label
      >
      <span class="count num"
        >{live.length + done.length}{#if filtering}
          of {everything.length}{/if}</span
      >
    </div>

    <section class="panel">
      <p class="sec-label">
        / In flight
        <span class="win"
          >{data.board.snapshotted_at
            ? `as of ${isoClock(data.board.snapshotted_at)} UTC`
            : "no snapshot yet"} · {live.length}</span
        >
      </p>
      <ul class="rows">
        {@render ledgerHead()}
        {#each live as task, index (`${task.issue_number}-${index}`)}
          {@render row(
            task,
            task.state === "queued" ? "–" : relative(task.admitted_at, now),
          )}
        {/each}
      </ul>
      {#if !live.length}
        <p class="empty">
          {filtering ? "nothing matches" : "nothing in the lane"}
        </p>
      {/if}
    </section>

    <section class="panel">
      <p class="sec-label">
        / Completed
        <span class="win"
          >{done.length
            ? `${pages.start}–${pages.end} of ${done.length}`
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
      {#if !done.length}
        <p class="empty">
          {filtering ? "nothing matches" : "nothing finished yet"}
        </p>
      {/if}
      {#if pages.pageCount > 1}
        <div class="pager">
          <span class="num">page {pages.page + 1} of {pages.pageCount}</span>
          <span
            ><button
              type="button"
              onclick={() => set({ page: pages.page })}
              disabled={pages.page === 0}>prev</button
            ><button
              type="button"
              onclick={() => set({ page: pages.page + 2 })}
              disabled={pages.page >= pages.pageCount - 1}>next</button
            ></span
          >
        </div>
      {/if}
    </section>
  </div>
</main>
