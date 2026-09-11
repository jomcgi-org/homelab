<script>
  import { SchemeToggle, Seo } from "$lib/public/components";
  import {
    activityRow,
    attemptWord,
    clip,
    commitUrl,
    diffLines,
    plural,
    sessionHref,
    sessionSpec,
    turnMeta,
  } from "$lib/public/factory/activity-view.js";
  import "$lib/public/factory/factory.css";
  import "$lib/public/factory/activity.css";
  import Trail from "../../../../../Trail.svelte";

  let { data } = $props();

  // Every disclosure on this page is keyed by turn and row, so one record of
  // what is open serves the hunks, the patches, the long prompts and the long
  // activity lists alike. The page is a finished record, so nothing here
  // refetches.
  let opened = $state({});

  // The list stays complete here, because this is the record: the task page is
  // the place that summarises. But a Codex-runtime turn records over a hundred
  // commands, so the tail of one opens on a toggle rather than pushing the
  // reply off the screen.
  const ACTS_CLIP = 12;

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

  const toggle = (key) => (opened[key] = !opened[key]);
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

{#snippet long(text, key, cls)}
  {@const cut = clip(text ?? "")}
  <p class={cls || undefined}>
    {cut.clipped && !opened[key]
      ? cut.head
      : (text ?? "")}{#if cut.clipped}<button
        class="more-tog"
        type="button"
        aria-expanded={Boolean(opened[key])}
        onclick={() => toggle(key)}
        >{opened[key]
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
          {
            label: `#${data.task.issue_number}`,
            href: `/slop/factory/activity/${data.task.issue_number}`,
          },
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
      <div>
        <div class="id">
          session ·
          <a href={`/slop/factory/activity/${data.task.issue_number}`}
            >task #{data.task.issue_number}</a
          >
          · node {data.node.node_key} · attempt {data.attempt.attempt} of {policy.max_attempts}
        </div>
        <h2>
          {data.node.node_key}
          <span class="who">{data.node.model} · {word}</span>
        </h2>
        <p class="brief lede">{data.task.title}</p>
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
        <span class="win"
          >oldest first · the instruction in grey, what ran, the reply · open an
          edit for its hunk, a turn for its patch</span
        >
      </p>
      {#if data.turns.length}
        <ol class="rec">
          {#each data.turns as turn, turnIndex (`${turn.seq}-${turnIndex}`)}
            {@const meta = turnMeta(turn)}
            {@const patchKey = `patch-${turn.seq}`}
            <li>
              <span class="tn">{turn.seq}</span>
              <div class="body">
                {@render long(turn.prompt, `ask-${turn.seq}`, "ask")}
                {#if turn.activities?.length}
                  {@const actsKey = `acts-${turn.seq}`}
                  {@const all = turn.activities}
                  <!-- A prefix, never a filter: row N is the same activity
                       open or closed, so its hunk key survives expansion. -->
                  <ul class="acts" id={actsKey}>
                    {#each opened[actsKey] ? all : all.slice(0, ACTS_CLIP) as activity, index (index)}
                      {@const row = activityRow(activity, turn.diff)}
                      {@const hunkKey = `hunk-${turn.seq}-${index}`}
                      <li class:open={opened[hunkKey]}>
                        {#if row.hunk}
                          <button
                            class="a x"
                            type="button"
                            aria-expanded={Boolean(opened[hunkKey])}
                            aria-controls={hunkKey}
                            onclick={() => toggle(hunkKey)}
                          >
                            <span class="ty">{row.type}</span>
                            <span class="what">{row.what}</span>
                            <span class="tog" aria-hidden="true"
                              >{opened[hunkKey] ? "−" : "+"}</span
                            >
                          </button>
                          <div
                            class="hunk"
                            id={hunkKey}
                            hidden={!opened[hunkKey]}
                          >
                            {#each diffLines(row.hunk) as line, lineIndex (lineIndex)}
                              <span class="ln {line.cls}">{line.text}</span>
                            {/each}
                          </div>
                        {:else}
                          <div class="a">
                            <span class="ty">{row.type}</span>
                            <span class="what">{row.what}</span>
                            <span class="tog" aria-hidden="true"></span>
                          </div>
                        {/if}
                      </li>
                    {/each}
                  </ul>
                  {#if all.length > ACTS_CLIP}
                    <button
                      class="more-tog acts-tog"
                      type="button"
                      aria-expanded={Boolean(opened[actsKey])}
                      aria-controls={actsKey}
                      onclick={() => toggle(actsKey)}
                      >{opened[actsKey]
                        ? "hide −"
                        : `show all (${all.length}) +`}</button
                    >
                  {/if}
                {/if}
                {@render long(turn.result_text, `say-${turn.seq}`, "")}
                {#if turn.rationale?.raw}
                  <p class="why">{turn.rationale.raw}</p>
                {/if}
                <div class="meta">
                  {#each meta as part, index (index)}
                    {#if part.sha}
                      <span
                        >{part.text}<a class="sha" href={commitUrl(part.sha)}
                          >{part.sha}</a
                        >{#if part.baseSha}
                          on <a class="sha" href={commitUrl(part.baseSha)}
                            >{part.baseSha}</a
                          >{/if}</span
                      >
                    {:else if part.stat}
                      <span class="stat"
                        >{plural(part.stat.files, "file")}
                        <b>+{part.stat.additions}</b>
                        <s>−{part.stat.deletions}</s></span
                      >
                      <button
                        type="button"
                        aria-expanded={Boolean(opened[patchKey])}
                        aria-controls={patchKey}
                        onclick={() => toggle(patchKey)}
                        >patch {opened[patchKey] ? "−" : "+"}</button
                      >
                    {:else if part.strong}
                      <span>{part.text}<b>{part.strong}</b></span>
                    {:else if part.bad}
                      <span class="bad">{part.text}</span>
                    {:else}
                      <span>{part.text}</span>
                    {/if}
                  {/each}
                </div>
                {#if turn.diff}
                  <div
                    class="hunk patch"
                    id={patchKey}
                    hidden={!opened[patchKey]}
                  >
                    {#each diffLines(turn.diff) as line, lineIndex (lineIndex)}
                      <span class="ln {line.cls}">{line.text}</span>
                    {/each}
                  </div>
                {/if}
              </div>
            </li>
          {/each}
        </ol>
      {:else}
        <p class="empty">
          {word === "running"
            ? "Running. The first turn has not returned yet."
            : "No turns were recorded for this attempt."}
        </p>
      {/if}
    </section>

    <a class="back" href={`/slop/factory/activity/${data.task.issue_number}`}
      >← task #{data.task.issue_number}</a
    >
  </div>
</main>
