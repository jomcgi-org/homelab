<script>
  import { Seo } from "$lib/public/components";
  import {
    barChartSvg,
    lastDays,
    sparkSvg,
  } from "$lib/public/factory/charts.js";
  import "$lib/public/factory/factory.css";
  import Trail from "../Trail.svelte";

  let { data } = $props();
  let sort = $state("date");
  let direction = $state(-1);
  let prPage = $state(0);
  const pageSize = 8;

  const number = (value) => Number(value ?? 0).toLocaleString();
  const short = (value) => {
    const n = Number(value ?? 0);
    if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
    if (n >= 1e3) return `${(n / 1e3).toFixed(0)}k`;
    return String(n);
  };
  const day = (value) => value?.slice(5, 10).replace("-", "·") ?? "";
  const typeKeys = [
    "feat",
    "fix",
    "docs",
    "chore",
    "test",
    "refactor",
    "other",
  ];

  function modelBucket(model) {
    const name = String(model).toLowerCase();
    if (name.includes("luna")) return "luna";
    if (name.includes("sol") || name.includes("terra")) return "codex";
    if (name.includes("claude")) return "claude";
    if (name.includes("spark")) return "spark";
    return "rest";
  }

  function activityDaily(rows) {
    const byDay = new Map();
    for (const row of rows) {
      const value = byDay.get(row.day) ?? {
        d: row.day,
        luna: 0,
        codex: 0,
        claude: 0,
        spark: 0,
        rest: 0,
        sessions: 0,
        input_tokens: 0,
        output_tokens: 0,
        cost_usd: 0,
      };
      value[modelBucket(row.model)] += Number(row.sessions ?? 0);
      value.sessions += Number(row.sessions ?? 0);
      value.input_tokens += Number(row.input_tokens ?? 0);
      value.output_tokens += Number(row.output_tokens ?? 0);
      value.cost_usd += Number(row.cost_usd ?? 0);
      byDay.set(row.day, value);
    }
    return [...byDay.values()].sort((a, b) => a.d.localeCompare(b.d));
  }

  function mergeDaily(rows) {
    return rows.map((row) => ({
      ...row,
      n: typeKeys.reduce((sum, key) => sum + Number(row[key] ?? 0), 0),
      rest:
        Number(row.chore ?? 0) +
        Number(row.test ?? 0) +
        Number(row.refactor ?? 0) +
        Number(row.other ?? 0),
    }));
  }

  function lineDaily(rows) {
    const byDay = new Map();
    for (const row of rows) {
      const d = row.merged_at?.slice(0, 10);
      if (!d) continue;
      const value = byDay.get(d) ?? { d, add: 0, del: 0 };
      value.add += Number(row.additions ?? 0);
      value.del += Number(row.deletions ?? 0);
      byDay.set(d, value);
    }
    return [...byDay.values()];
  }

  function factDaily(rows) {
    const byDay = new Map();
    for (const row of rows) {
      const d = row.observed_at?.slice(0, 10);
      if (!d) continue;
      const value = byDay.get(d) ?? { d, v: 0, u: 0, n: 0 };
      if (row.verification_state === "verified") value.v += 1;
      if (row.verification_state === "unverified") value.u += 1;
      value.n = value.v + value.u;
      byDay.set(d, value);
    }
    return [...byDay.values()].sort((a, b) => a.d.localeCompare(b.d));
  }

  const sessions = $derived(activityDaily(data.activity.daily));
  const merges = $derived(mergeDaily(data.merges.daily));
  const lines = $derived(lineDaily(data.merges.week));
  const allFactDays = $derived(factDaily(data.facts));
  const today = $derived(
    [...sessions, ...merges, ...allFactDays]
      .map((row) => row.d)
      .sort()
      .at(-1) ?? new Date().toISOString().slice(0, 10),
  );
  const facts = $derived.by(() => {
    const first = new Date(`${today}T00:00:00Z`);
    first.setUTCDate(first.getUTCDate() - 29);
    const start = first.toISOString().slice(0, 10);
    return allFactDays.filter((row) => row.d >= start && row.d <= today);
  });
  const factTotals = $derived({
    verified: data.facts.filter((row) => row.verification_state === "verified")
      .length,
    unverified: data.facts.filter(
      (row) => row.verification_state === "unverified",
    ).length,
  });
  const fresh = $derived(
    data.facts
      .map((row) => row.observed_at)
      .filter(Boolean)
      .sort()
      .at(-1),
  );
  const stats = $derived([
    {
      key: "Live",
      value: number(data.activity.now.active_last_hour),
      subline: `${number(data.activity.now.sessions_today)} sessions today`,
      spark: lastDays(sessions, "sessions", today),
    },
    {
      key: "Merged, 7d",
      value: number(data.merges.totals.n_7d),
      subline: `${number(data.merges.totals.agent_7d)} by agents`,
      spark: lastDays(merges, "n", today),
    },
    {
      key: "Lines, 7d",
      additions: short(data.merges.totals.add_7d),
      deletions: short(data.merges.totals.del_7d),
      subline: `net ${Number(data.merges.totals.add_7d ?? 0) - Number(data.merges.totals.del_7d ?? 0) >= 0 ? "+" : "−"}${short(Math.abs(Number(data.merges.totals.add_7d ?? 0) - Number(data.merges.totals.del_7d ?? 0)))}`,
      spark: lastDays(lines, "add", today),
    },
    {
      key: "Tokens, 7d",
      value: short(data.activity.totals_7d.input_tokens),
      subline: `${short(data.activity.totals_7d.output_tokens)} out`,
      spark: lastDays(sessions, "input_tokens", today),
    },
    {
      key: "Cost, 7d",
      value: `$${Number(data.activity.totals_7d.cost_usd ?? 0).toFixed(0)}`,
      unit: "metered",
      subline:
        data.activity.totals_7d.list_cost_usd == null
          ? "Codex on subscription"
          : `list $${Number(data.activity.totals_7d.list_cost_usd).toFixed(0)}`,
      spark: lastDays(sessions, "cost_usd", today),
    },
    {
      key: "Facts",
      value: number(factTotals.verified + factTotals.unverified),
      subline: `${number(factTotals.verified)} verified${fresh ? ` · ${fresh.slice(5, 10).replace("-", "·")}` : ""}`,
      spark: lastDays(facts, "n", today),
    },
  ]);

  const typeBreakdown = $derived(
    Object.entries(
      data.merges.week.reduce((out, row) => {
        out[row.type] = (out[row.type] ?? 0) + 1;
        return out;
      }, {}),
    )
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 4),
  );
  const scopeBreakdown = $derived(
    Object.entries(
      data.merges.week.reduce((out, row) => {
        const scope = row.scope || "other";
        out[scope] = (out[scope] ?? 0) + 1;
        return out;
      }, {}),
    )
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 4),
  );

  const sortedPrs = $derived.by(() => {
    const key = {
      date: (row) => row.merged_at,
      type: (row) => row.type,
      area: (row) => row.scope || "~",
      lines: (row) => Number(row.additions) + Number(row.deletions),
    }[sort];
    return [...data.merges.week].sort((a, b) => {
      const av = key(a);
      const bv = key(b);
      return (
        (av < bv ? -1 : av > bv ? 1 : 0) * direction ||
        b.merged_at.localeCompare(a.merged_at)
      );
    });
  });
  const pageCount = $derived(
    Math.max(1, Math.ceil(sortedPrs.length / pageSize)),
  );
  const visiblePrs = $derived(
    sortedPrs.slice(prPage * pageSize, (prPage + 1) * pageSize),
  );

  function changeSort(next) {
    if (sort === next) direction *= -1;
    else {
      sort = next;
      direction = next === "date" || next === "lines" ? -1 : 1;
    }
    prPage = 0;
  }

  function cleanTitle(title) {
    return title.replace(/^\w+(\(.*?\))?!?:\s*/, "");
  }
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
        <p>What the agents merged, what it cost, what they learned.</p>
      </div>
      <div class="mast-trails">
        <Trail page="factory" />
        <nav class="view-tabs" aria-label="Factory views">
          <a class="here" href="/slop/factory" aria-current="page">overview</a>
          <a href="/slop/factory/record">record</a>
        </nav>
      </div>
    </header>

    <div class="home">
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
            <div class="s">{stat.subline}</div>
          </div>
        {/each}
      </div>

      <div class="charts">
        <div class="chart">
          <p class="sec-label">/ Sessions per day</p>
          {@html barChartSvg(
            "sessions",
            sessions,
            ["luna", "codex", "claude", "spark", "rest"],
            [
              "var(--tone-gpu)",
              "var(--tone-cache)",
              "var(--tone-hot)",
              "var(--accent)",
              "hatch",
            ],
          )}
          <p class="legend">
            <span><i class="gpu"></i>luna</span><span
              ><i class="cache"></i>sol, terra</span
            ><span><i class="hot"></i>claude</span><span
              ><i class="amber"></i>spark</span
            ><span><i class="lh"></i>other</span>
          </p>
        </div>
        <div class="chart">
          <p class="sec-label">/ Merged to main per day</p>
          {@html barChartSvg(
            "merges",
            merges.slice(1),
            ["feat", "fix", "docs", "rest"],
            ["var(--tone-ram)", "var(--tone-gpu)", "var(--accent)", "hatch"],
          )}
          <p class="legend">
            <span><i class="ram"></i>feat</span><span
              ><i class="gpu"></i>fix</span
            ><span><i class="amber"></i>docs</span><span
              ><i class="lh"></i>chore, test, other</span
            >
          </p>
        </div>
        <div class="chart">
          <p class="sec-label">/ Facts written per day</p>
          {@html barChartSvg(
            "facts",
            facts,
            ["v", "u"],
            ["var(--tone-ram)", "hatch"],
          )}
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
            {#each visiblePrs as pr (pr.number)}
              <li>
                <span class:feat={pr.type === "feat"} class="ty">{pr.type}</span
                >
                <span
                  ><a
                    href={`https://github.com/jomcgi/homelab/pull/${pr.number}`}
                    >{cleanTitle(pr.title)}</a
                  >{#if pr.scope}
                    <span class="sc">· {pr.scope}</span>{/if}</span
                >
                <span class="ch"
                  ><b>+{short(pr.additions)}</b>
                  <s>−{short(pr.deletions)}</s></span
                >
                <span class="dt">{day(pr.merged_at)}</span>
              </li>
            {/each}
            <li class="pager">
              <span
                >{sortedPrs.length ? prPage * pageSize + 1 : 0}–{Math.min(
                  sortedPrs.length,
                  (prPage + 1) * pageSize,
                )} of {sortedPrs.length} this week</span
              >
              <span
                ><button
                  type="button"
                  onclick={() => (prPage -= 1)}
                  disabled={prPage === 0}>prev</button
                ><button
                  type="button"
                  onclick={() => (prPage += 1)}
                  disabled={prPage >= pageCount - 1}>next</button
                ></span
              >
            </li>
          </ol>
        </div>

        <div class="ask">
          <p class="sec-label">/ Ask the record</p>
          <div class="sheet">
            <div class="thread">
              <div class="turn you">
                <div class="who">You</div>
                <p>What is blocking embervm right now?</p>
              </div>
              <div class="turn">
                <div class="who">Record</div>
                <p>
                  An unresolved quarantine blocks the whole drain tick, not just
                  one job. Whether it also permits stale-cycle replacement is
                  contradicted and unsettled.
                </p>
                <ul class="grounds">
                  <li>
                    <i class="mark unverified"></i><span
                      ><b>Unresolved quarantine blocks the entire drain tick</b> ·
                      ember session · today</span
                    >
                  </li>
                  <li>
                    <i class="mark contradicts"></i><span
                      ><b
                        >Unresolved quarantine can still permit stale-cycle
                        replacement</b
                      > · contradicts a verified fact</span
                    >
                  </li>
                </ul>
              </div>
            </div>
            <a class="ask-link" href="/app/notes"
              >Ask about a project, a deploy, a failure <span>Open chat</span
              ></a
            >
          </div>
          <p class="note">
            Grounded on the record. No tools, no cluster access.
          </p>
        </div>
      </div>
    </div>
  </div>
</main>
