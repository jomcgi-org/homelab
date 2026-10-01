<script>
  import { SchemeToggle, Seo } from "$lib/public/components";
  import {
    attemptWord,
    sessionHref,
    sessionSpec,
    plural,
  } from "$lib/public/factory/activity-view.js";
  import Turn from "$lib/public/factory/Turn.svelte";
  import "$lib/public/factory/factory.css";
  import "$lib/public/factory/activity.css";
  import Trail from "../../../../../Trail.svelte";

  let { data } = $props();

  // The page is a finished record, so nothing here refetches.
  const session = $derived(data.session ?? {});
  const policy = $derived(data.policy ?? {});
  // The attempt's status, not the session row's: the marks are the attempt
  // vocabulary (admitted, succeeded, failed, uncertain), and this is the same
  // word the step row on the task page shows for the same run.
  const word = $derived(attemptWord(data.attempt.status));
  const spec = $derived(
    sessionSpec({
      key: session.key ?? data.attempt.session_key,
      model: session.model ?? data.node.model,
      status: data.attempt.status,
      turn_count: session.turn_count ?? data.turns?.length ?? 0,
      cost_usd: session.cost_usd ?? data.attempt.cost_usd,
      guest_bound: session.guest_bound,
      created_at: session.created_at,
      last_turn_at: session.last_turn_at,
      terminal_reason: session.terminal_reason,
    }),
  );
  const taskHref = $derived(`/slop/factory/activity/${data.task.issue_number}`);
</script>

<Seo
  title={`#${data.task.issue_number} ${data.node.node_key} · attempt ${data.attempt.attempt} · Factory activity · jomcgi.dev`}
  description={`The full record of one factory attempt on issue #${data.task.issue_number}: every turn, what it ran, and what it changed.`}
  path={sessionHref(
    data.task.issue_number,
    data.node.node_key,
    data.attempt.attempt,
  )}
/>

<main class="td factory-page activity-page">
  <div class="frame">
    <header class="masthead">
      <h1 class="sr-only">Ember Software Factory</h1>
      <Trail
        crumbs={[
          { label: "factory", href: "/slop/factory" },
          { label: "activity", href: "/slop/factory/activity" },
          { label: `#${data.task.issue_number}`, href: taskHref },
          { label: `${data.node.node_key} · ${data.attempt.attempt}` },
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

    <div class="sess-head">
      <div class="title-block">
        <div class="id">
          <a href={taskHref}>#{data.task.issue_number}</a> · {data.node
            .node_key} · attempt {data.attempt.attempt} of {policy.max_attempts}
        </div>
        <h2>
          {data.node.node_key}
          <span class="who">{data.node.model} · {word}</span>
        </h2>
        <p class="lede"><a href={taskHref}>{data.task.title}</a></p>
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
            </span>
          </div>
        {/each}
      </div>
    </div>

    <section class="panel">
      <p class="sec-label">
        / Turns
        <span class="win">{plural(data.turns.length, "turn")}</span>
      </p>
      {#if data.turns.length}
        <ol class="rec">
          {#each data.turns as turn, turnIndex (`${turn.seq}-${turnIndex}`)}
            <li><Turn {turn} full={true} /></li>
          {/each}
        </ol>
      {:else}
        <p class="empty">
          {word === "running" ? "running · no turn yet" : "no turns recorded"}
        </p>
      {/if}
    </section>

    <a class="back" href={taskHref}>← #{data.task.issue_number}</a>
  </div>
</main>
