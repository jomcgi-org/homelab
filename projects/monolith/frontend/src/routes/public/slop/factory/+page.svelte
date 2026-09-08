<script>
  import { Seo } from "$lib/public/components";
  import { barChartSvg, sparkSvg } from "$lib/public/factory/charts.js";
  import {
    activitySeries,
    breakdown,
    cleanPullTitle,
    factSeries,
    formatCount,
    formatSpend,
    goalSummary,
    lineSeries,
    mergeSeries,
    paginate,
    shortNumber,
    snapshotFreshness,
    sortPullRequests,
    spendSeries,
    tileDerivations,
  } from "$lib/public/factory/model.js";
  import "$lib/public/factory/factory.css";
  import Trail from "../Trail.svelte";

  let { data } = $props();
  let sort = $state("date");
  let direction = $state(-1);
  let prPage = $state(0);
  const pageSize = 8;

  const day = (value) => value?.slice(5, 10).replace("-", "·") ?? "";
  const today = new Date().toISOString().slice(0, 10);
  const initialFresh = snapshotFreshness(
    data.merges.snapshotted_at,
    data.merges.snapshotted_at,
  );
  let fresh = $state(initialFresh);
  let freshLabel = $state(
    initialFresh.clock
      ? `snapshot ${initialFresh.clock} UTC`
      : "snapshot unknown",
  );
  const sessions = $derived(
    activitySeries([
      ...(data.activity.daily ?? []),
      ...(data.activity.local_daily ?? []),
    ]),
  );
  const spend = $derived(spendSeries(data.activity.spend_daily ?? []));
  const merges = $derived(mergeSeries(data.merges.daily));
  const lines = $derived(lineSeries(data.merges.week));
  const facts = $derived(factSeries(data.facts, today));
  const tiles = $derived(
    tileDerivations(
      data.activity,
      data.merges,
      data.facts,
      { sessions, spend, merges, lines, facts },
      today,
    ),
  );
  const goalData = $derived(
    goalSummary(data.merges.week, data.merges.snapshotted_at),
  );
  const stats = $derived([
    {
      key: "Live",
      value: formatCount(tiles.live.value),
      subline: `${formatCount(tiles.live.sessionsToday)} sessions today`,
      spark: tiles.live.spark,
    },
    {
      key: "Sessions, 7d",
      value: formatCount(tiles.sessions.value),
      spark: tiles.sessions.spark,
    },
    {
      key: "Merged, 7d",
      value: formatCount(tiles.merged.value),
      subline: `${formatCount(tiles.merged.agent)} by agents · +${shortNumber(tiles.lines.additions)} −${shortNumber(tiles.lines.deletions)}`,
      spark: tiles.merged.spark,
    },
    {
      key: "Tokens, 7d",
      value: shortNumber(tiles.tokens.input),
      subline: `${shortNumber(tiles.tokens.output)} out`,
      spark: tiles.tokens.spark,
    },
    {
      key: "Spend, 7d",
      value: formatSpend(tiles.spend.value),
      subline: tiles.spend.maxSession
        ? `${formatSpend(tiles.spend.maxSession)} priciest session`
        : "",
      spark: tiles.spend.spark,
    },
    {
      key: "Facts",
      value: formatCount(tiles.facts.value),
      spark: tiles.facts.spark,
    },
  ]);

  const typeBreakdown = $derived(
    breakdown(data.merges.week, (row) => row.type),
  );
  const scopeBreakdown = $derived(
    breakdown(data.merges.week, (row) => row.scope),
  );

  const sortedPrs = $derived(
    sortPullRequests(data.merges.week, sort, direction),
  );
  const prRows = $derived(paginate(sortedPrs, prPage, pageSize));

  function changeSort(next) {
    if (sort === next) direction *= -1;
    else {
      sort = next;
      direction = next === "date" || next === "lines" ? -1 : 1;
    }
    prPage = 0;
  }

  function goalMeta(goal) {
    return [
      ...goal.types.map(([type, count]) => `${count} ${type}`),
      `+${shortNumber(goal.additions)} −${shortNumber(goal.deletions)}`,
    ].join(" / ");
  }

  $effect(() => {
    const refresh = () => {
      const next = snapshotFreshness(data.merges.snapshotted_at, new Date());
      fresh = next;
      freshLabel = next.clock
        ? `snapshot ${next.clock} UTC / ${next.label}`
        : "snapshot unknown";
    };
    refresh();
    const timer = setInterval(refresh, 60_000);
    return () => clearInterval(timer);
  });
</script>

<Seo
  title="Ember Software Factory · jomcgi.dev"
  description="What the agents merged, what it cost, what they learned."
  path="/slop/factory"
/>

<main class="td factory-page">
  <div class="frame">
    <header class="masthead">
      <div>
        <h1>Ember Software Factory</h1>
      </div>
      <div class="mast-trails">
        <Trail page="factory" />
        <nav class="view-tabs" aria-label="Factory views">
          <a class="here" href="/slop/factory" aria-current="page">overview</a>
          <a href="/slop/factory/context">context</a>
        </nav>
      </div>
    </header>

    <div class="home">
      {#if Object.values(data.unavailable).some(Boolean)}
        <p class="unavailable">Unavailable right now.</p>
      {/if}
      <div class="stats">
        {#each stats as stat}
          <div>
            <div class="k">{stat.key}</div>
            <div class="v num">
              {#if stat.additions}
                <span class="added">+{stat.additions}</span>
                <span class="deleted">−{stat.deletions}</span>
              {:else}
                {stat.value}{#if stat.unit}<small>{stat.unit}</small>{/if}
              {/if}
            </div>
            {@html sparkSvg(stat.spark)}
            {#if stat.subline}<div class="s">{stat.subline}</div>{/if}
          </div>
        {/each}
      </div>

      <div class="charts">
        <div class="chart">
          <p class="sec-label">/ Sessions per day</p>
          {#if data.unavailable.activity}
            <p class="none">Nothing passes the current filter.</p>
          {:else}
            {@html barChartSvg(
              "sessions",
              sessions,
              ["luna", "codex", "claude", "spark", "other"],
              [
                "var(--tone-gpu)",
                "var(--tone-cache)",
                "var(--tone-hot)",
                "var(--ink-3)",
                "hatch",
              ],
            )}
          {/if}
          <p class="legend">
            <span><i class="gpu"></i>luna</span><span
              ><i class="cache"></i>sol, terra</span
            ><span><i class="hot"></i>claude</span><span
              ><i class="spark"></i>spark</span
            ><span><i class="lh"></i>other</span>
          </p>
        </div>
        <div class="chart">
          <p class="sec-label">/ Merged to main per day</p>
          {#if data.unavailable.merges}
            <p class="none">Nothing passes the current filter.</p>
          {:else}
            {@html barChartSvg(
              "merges",
              merges,
              ["feat", "fix", "docs", "rest"],
              ["var(--tone-ram)", "var(--tone-gpu)", "var(--accent)", "hatch"],
            )}
          {/if}
          <p class="legend">
            <span><i class="ram"></i>feat</span><span
              ><i class="gpu"></i>fix</span
            ><span><i class="docs"></i>docs</span><span
              ><i class="lh"></i>chore, test, other</span
            >
          </p>
        </div>
        <div class="chart">
          <p class="sec-label">/ Facts written per day</p>
          {#if data.unavailable.facts}
            <p class="none">Nothing passes the current filter.</p>
          {:else}
            {@html barChartSvg(
              "facts",
              facts,
              ["v", "u"],
              ["var(--tone-ram)", "hatch"],
            )}
          {/if}
          <p class="legend">
            <span><i class="ram"></i>verified</span><span
              ><i class="lh"></i>unverified</span
            >
          </p>
        </div>
      </div>

      <div class="lower">
        <div class="outcomes">
          <p class="sec-label">/ Merged this week</p>
          <div class="breakdown">
            {#each [["by type", typeBreakdown], ["by area", scopeBreakdown]] as [heading, rows]}
              <div>
                <div class="head">{heading}</div>
                {#each rows as [label, count]}
                  <div class="row">
                    <span>{label}</span>
                    <span class="bar"
                      ><i
                        style={`width:${rows[0]?.[1] ? (count / rows[0][1]) * 100 : 0}%`}
                      ></i></span
                    >
                    <span class="t num">{count}</span>
                  </div>
                {/each}
              </div>
            {/each}
          </div>
          <ol class="prs">
            <li class="hd">
              {#each [["type", "type"], ["area", "title · area"], ["lines", "lines"], ["date", "day"]] as [key, label], index}
                <button
                  class:on={sort === key}
                  class:r={index > 1}
                  type="button"
                  onclick={() => changeSort(key)}
                  >{label}{sort === key
                    ? direction < 0
                      ? " ↓"
                      : " ↑"
                    : ""}</button
                >
              {/each}
            </li>
            {#each prRows.rows as pr (pr.number)}
              <li>
                <span class:feat={pr.type === "feat"} class="ty">{pr.type}</span
                >
                <span
                  ><a
                    href={`https://github.com/jomcgi/homelab/pull/${pr.number}`}
                    >{cleanPullTitle(pr.title)}</a
                  >{#if pr.scope}
                    <span class="sc">· {pr.scope}</span>{/if}</span
                >
                <span class="ch"
                  ><b>+{shortNumber(pr.additions)}</b>
                  <s>−{shortNumber(pr.deletions)}</s></span
                >
                <span class="dt">{day(pr.merged_at)}</span>
              </li>
            {/each}
            <li class="pager">
              <span
                >{prRows.start}–{prRows.end} of {sortedPrs.length} this week</span
              >
              <span
                ><button
                  type="button"
                  onclick={() => (prPage -= 1)}
                  disabled={prPage === 0}>prev</button
                ><button
                  type="button"
                  onclick={() => (prPage += 1)}
                  disabled={prPage >= prRows.pageCount - 1}>next</button
                ></span
              >
            </li>
          </ol>
          {#if data.unavailable.merges}
            <p class="none">None recorded.</p>
          {/if}
        </div>

        <div class="goals">
          <p class="sec-label">
            / Goal summary
            <span class="win">last {goalData.windowHours}h</span>
            <span class="fresh" class:stale={fresh.stale}>{freshLabel}</span>
          </p>
          {#if goalData.goals.length}
            <ol class="goal-list">
              {#each goalData.goals as goal, index}
                <li>
                  <div class="goal-head">
                    <span class="rank num"
                      >{String(index + 1).padStart(2, "0")}</span
                    >
                    <span class="goal-name"
                      ><strong>{goal.area}</strong><span>{goal.focus}</span
                      ></span
                    >
                    <span class="bar" aria-hidden="true"
                      ><i style={`width:${goal.share * 100}%`}></i></span
                    >
                    <span class="merge-count num">{goal.merged} merged</span>
                  </div>
                  <p class="goal-meta">{goalMeta(goal)}</p>
                  <ul class="goal-recent">
                    {#each goal.recent as pull}
                      <li>
                        <a
                          href={`https://github.com/jomcgi/homelab/pull/${pull.number}`}
                          >{pull.title}</a
                        >
                      </li>
                    {/each}
                  </ul>
                </li>
              {/each}
            </ol>
          {:else}
            <p class="none">
              Nothing merged in the last {goalData.windowHours} hours.
            </p>
          {/if}
        </div>
      </div>
    </div>
  </div>
</main>
