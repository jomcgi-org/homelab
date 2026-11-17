import { useEffect, useState } from "react";
import { BarChart, Scatter, TaskMatrix } from "./leaderboard-charts";
import { Selector } from "./leaderboard-selector";
import {
  METRICS,
  fmtDate,
  hardRate,
  indexRoles,
  indexRows,
  kfmt,
  money,
  orderedTasks,
  parseSelection,
  providerSlot,
  rank,
  secs,
  serializeSelection,
  shortName,
} from "./leaderboard-model";
import type {
  IndexedModel,
  Leaderboard,
  MetricKey,
  Model,
} from "./leaderboard-model";

const COLS = [
  ["rank", "#"],
  ["name", "model"],
  ["hard", "hard"],
  ["floor", "floor"],
  ["cost", "$ / task"],
  ["solve", "$ / solve"],
  ["wall", "wall / task"],
  ["tokens", "tokens / task"],
  ["turns", "steps"],
  ["tools", "tool calls ok"],
] as const;
type SortKey = (typeof COLS)[number][0];
const INDEX_Y = {
  get: (m: IndexedModel) => m.index_value,
  title: "Agent index",
  axis: "agent index",
  point: "index",
  corner: "Claude's index, for less",
};

export function LeaderboardContent({ data: lb }: { data: Leaderboard }) {
  const { models, tasks } = lb;
  const keyed = orderedTasks(tasks);
  const ranked = rank(models);
  // The first browser render matches the static HTML. Apply a linked selection
  // after hydration, and on back/forward navigation, without a server request.
  const [selected, setSelected] = useState(() => parseSelection(models, null));
  useEffect(() => {
    const read = () =>
      setSelected(
        parseSelection(
          models,
          new URL(window.location.href).searchParams.get("m"),
        ),
      );
    read();
    window.addEventListener("popstate", read);
    return () => window.removeEventListener("popstate", read);
  }, [models]);
  function choose(next: Set<string>) {
    setSelected(next);
    const url = new URL(window.location.href);
    const value = serializeSelection(models, next);
    if (value) url.searchParams.set("m", value);
    else url.searchParams.delete("m");
    window.history.replaceState(window.history.state, "", url);
  }
  const [hot, setHot] = useState<string | null>(null);
  const shown = ranked.filter((m) => selected.has(m.id));
  const chart = { models: shown, hot, onHover: setHot };
  const [sortKey, setSortKey] = useState<SortKey>("rank");
  const [sortDir, setSortDir] = useState(1);
  const SORT_GET: Record<SortKey, (m: Model) => string | number> = {
    rank: (m) => ranked.indexOf(m),
    name: shortName,
    hard: (m) => -hardRate(m),
    floor: (m) => -(m.floor_n ? m.floor_pass / m.floor_n : 0),
    cost: (m) => m.cost_usd ?? 0,
    solve: (m) => m.cost_per_solve_usd ?? Infinity,
    wall: (m) => m.mean_latency_ms ?? 0,
    tokens: (m) => m.mean_tokens ?? 0,
    turns: (m) => m.mean_turns ?? 0,
    tools: (m) => -(m.tool_use_ok ?? 0),
  };
  const rows = [...shown].sort((a, b) => {
    const x = SORT_GET[sortKey](a),
      y = SORT_GET[sortKey](b);
    return (
      sortDir *
      (typeof x === "string" ? x.localeCompare(String(y)) : x - Number(y))
    );
  });
  function sortBy(key: SortKey) {
    if (sortKey === key) setSortDir(-sortDir);
    else {
      setSortKey(key);
      setSortDir(1);
    }
  }
  const index = lb.index;
  const roles = indexRoles(index);
  const [role, setRole] = useState<string | null>(null);
  const activeRole = role ?? roles[0] ?? "";
  const indexed = indexRows(shown, index, activeRole);
  const picks = index?.picks?.[activeRole] ?? {};
  const nameOf = (id?: string) => {
    const m = models.find((x) => x.id === id);
    return m ? shortName(m) : (id ?? "n/a");
  };
  const leader = ranked[0];
  const cheapestPerfect = [...models]
    .filter(
      (m) =>
        hardRate(m) >= 1 &&
        m.role !== "anchor" &&
        (m.cost_per_solve_usd ?? 0) > 0,
    )
    .sort((a, b) => a.cost_per_solve_usd! - b.cost_per_solve_usd!)[0];
  const qualified = models.filter((m) => m.qualified).length;
  const hardCount = tasks.filter((t) => t.tier === "hard").length;
  const realCount = tasks.filter((t) => t.real_test).length;
  return (
    <div className="lb-page">
      <div className="frame">
        <div className="lead">
          <h1>LLM leaderboard</h1>
          <p>
            Models doing real tasks in my monorepos, graded by the repo's own
            tests.
          </p>
        </div>
        <div className="stats">
          <div>
            <div className="k">Leader</div>
            <div className="v">
              {leader ? `${leader.hard_pass}/${leader.hard_n}` : "n/a"}
              <small>hard</small>
            </div>
            <div className="m">{leader ? shortName(leader) : ""}</div>
          </div>
          <div>
            <div className="k">Cheapest full marks</div>
            <div className="v">
              {cheapestPerfect
                ? money(cheapestPerfect.cost_per_solve_usd)
                : "n/a"}
              <small>/ solve</small>
            </div>
            <div className="m">
              {cheapestPerfect
                ? `${shortName(cheapestPerfect)} · ${cheapestPerfect.hard_pass}/${cheapestPerfect.hard_n} hard`
                : ""}
            </div>
          </div>
          <div>
            <div className="k">Models</div>
            <div className="v">{models.length}</div>
            <div className="m">{qualified} cleared the floor</div>
          </div>
          <div>
            <div className="k">Tasks</div>
            <div className="v">{tasks.length}</div>
            <div className="m">
              {hardCount} hard · {realCount} repo-tested
            </div>
          </div>
          <div>
            <div className="k">Updated</div>
            <div className="v">{fmtDate(lb.generated_at)}</div>
            <div className="m">harness {lb.harness_version}</div>
          </div>
        </div>
        <Selector models={models} selected={selected} onChange={choose} />
        {!shown.length ? (
          <p className="panel empty">
            No models selected. Pick a preset or search above.
          </p>
        ) : (
          <>
            <section>
              <h2 className="sec-label">/ Headline metrics</h2>
              <div className="grid2">
                {(["hard", "cost", "wall", "tokens"] as MetricKey[]).map(
                  (metric) => (
                    <BarChart key={metric} {...chart} metric={metric} />
                  ),
                )}
              </div>
              <p className="caption">
                Means per task. Self-hosted: factory local rate, one RTX 4090.
              </p>
            </section>
            <section>
              <h2 className="sec-label">/ Capability vs efficiency</h2>
              <Scatter {...chart} />
            </section>
            {roles.length > 0 && (
              <section>
                <h2 className="sec-label">
                  / Agent index{" "}
                  <span className="aside">
                    per factory role, weights in index.yaml
                  </span>
                </h2>
                <div className="seg" role="group" aria-label="Factory role">
                  {roles.map((r) => (
                    <button
                      key={r}
                      type="button"
                      aria-pressed={activeRole === r}
                      onClick={() => setRole(r)}
                    >
                      {r}
                    </button>
                  ))}
                </div>
                {indexed.length ? (
                  <>
                    <Scatter {...chart} models={indexed} y={INDEX_Y} />
                    <div
                      className="panel table-wrap"
                      role="region"
                      aria-label="Agent index data"
                      tabIndex={0}
                    >
                      <table className="ranked" aria-label="Agent index">
                        <thead>
                          <tr>
                            <th className="l">model</th>
                            <th>index</th>
                            <th>95% CI</th>
                            <th>coverage</th>
                            <th>$ / task</th>
                            <th>wall / task</th>
                            <th className="l">low n</th>
                          </tr>
                        </thead>
                        <tbody>
                          {indexed.map((m) => (
                            <tr
                              key={m.id}
                              data-model={m.id}
                              className={hot === m.id ? "hot" : ""}
                            >
                              <td className="l">
                                <span className="nm">
                                  <i
                                    className="sw"
                                    data-slot={providerSlot(m.id)}
                                  />
                                  {shortName(m)}
                                </span>
                              </td>
                              <td className="num">
                                {m.index_value.toFixed(3)}
                              </td>
                              <td className="num">
                                {m.index_ci
                                  ? `${m.index_ci[0].toFixed(2)} to ${m.index_ci[1].toFixed(2)}`
                                  : "n/a"}
                              </td>
                              <td className="num">
                                {Math.round((m.index_coverage ?? 0) * 100)}%
                              </td>
                              <td className="num">{money(m.cost_usd)}</td>
                              <td className="num">
                                {secs((m.mean_latency_ms ?? 0) / 1000)}
                              </td>
                              <td className="l">{m.index_low_n.join(", ")}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="caption">
                      Best: {nameOf(picks.best)}. Cheapest near best:{" "}
                      {nameOf(picks.cheapest)}. Fastest near best:{" "}
                      {nameOf(picks.fastest)}. Low n: axis measured on fewer
                      than {index!.min_n} tasks.
                    </p>
                  </>
                ) : (
                  <p className="panel empty">
                    No selected model has a {activeRole} index.
                  </p>
                )}
              </section>
            )}
            <section>
              <h2 className="sec-label">/ Every task</h2>
              <TaskMatrix {...chart} tasks={tasks} />
            </section>
            <section>
              <h2 className="sec-label">
                / Ranked{" "}
                <span className="aside">
                  hard-task pass rate, then $ per solve
                </span>
              </h2>
              <div
                className="panel table-wrap"
                role="region"
                aria-label="Ranked model data"
                tabIndex={0}
              >
                <table
                  className={`ranked ${hot ? "hovering" : ""}`}
                  aria-label="Ranked models"
                >
                  <thead>
                    <tr>
                      {COLS.map(([key, label], i) => (
                        <th
                          key={key}
                          className={i < 2 ? "l" : ""}
                          aria-sort={
                            sortKey === key
                              ? sortDir > 0
                                ? "ascending"
                                : "descending"
                              : undefined
                          }
                        >
                          <button
                            type="button"
                            className={sortKey === key ? "on" : ""}
                            onClick={() => sortBy(key)}
                          >
                            {label}
                            {sortKey === key ? (sortDir > 0 ? " ↓" : " ↑") : ""}
                          </button>
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((m) => (
                      <tr
                        key={m.id}
                        data-model={m.id}
                        className={hot === m.id ? "hot" : ""}
                        onMouseEnter={() => setHot(m.id)}
                        onMouseLeave={() => setHot(null)}
                      >
                        <td className="l num rk">{ranked.indexOf(m) + 1}</td>
                        <td className="l">
                          <span className="nm">
                            <i className="sw" data-slot={providerSlot(m.id)} />
                            {shortName(m)}
                          </span>
                          <span className="slug">
                            {m.id}
                            {m.role === "anchor"
                              ? " · ceiling"
                              : m.self_hosted
                                ? " · self-hosted"
                                : ""}
                          </span>
                        </td>
                        <td className="num">
                          {m.hard_pass}/{m.hard_n}
                        </td>
                        <td className="num">
                          {m.floor_pass}/{m.floor_n}
                        </td>
                        <td className="num">
                          {money(m.cost_usd)}
                          {m.cost_estimated ? " est." : ""}
                        </td>
                        <td className="num">
                          {money(m.cost_per_solve_usd)}
                          {m.cost_estimated ? " est." : ""}
                        </td>
                        <td className="num">
                          {secs((m.mean_latency_ms ?? 0) / 1000)}
                        </td>
                        <td className="num">
                          {m.role === "anchor" ? "n/a" : kfmt(m.mean_tokens)}
                        </td>
                        <td className="num">
                          {m.role === "anchor"
                            ? "n/a"
                            : METRICS.turns.fmt(m.mean_turns)}
                        </td>
                        <td className="num">
                          {Math.round((m.tool_use_ok ?? 0) * 100)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="caption">Errored cells are excluded, not failed.</p>
            </section>
          </>
        )}
        <section>
          <h2 className="sec-label">
            / Tasks{" "}
            <span className="aside">
              passed / ran, across all {models.length} models
            </span>
          </h2>
          <ol className="panel tasks">
            {keyed.map((t) => (
              <li key={t.id} id={`task-${t.no}`}>
                <span className="n num">{t.no}</span>
                <span className="body">
                  <span className="id">{t.id}</span>
                  <span className="blurb">{t.blurb}</span>
                </span>
                <span className="meta">
                  {t.tier} · {t.real_test ? "repo test" : "behavioural"}
                </span>
                <span className="score num">
                  {t.passed}/{t.n}
                </span>
              </li>
            ))}
          </ol>
        </section>
        <footer className="caption method">
          <a href="https://github.com/jomcgi/homelab/tree/main/projects/model-bench">
            projects/model-bench
          </a>
        </footer>
      </div>
    </div>
  );
}
