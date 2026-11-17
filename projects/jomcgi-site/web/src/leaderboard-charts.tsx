import { Fragment, useState } from "react";
import {
  METRICS,
  cellState,
  orderedTasks,
  paretoFrontier,
  plottable,
  providerSlot,
  providersIn,
  shortName,
  sortByMetric,
} from "./leaderboard-model";
import type { MetricKey, Model, Task } from "./leaderboard-model";

export interface ChartProps<T extends Model = Model> {
  models: T[];
  hot: string | null;
  onHover: (id: string | null) => void;
}

export function BarChart({
  models,
  metric,
  hot,
  onHover,
}: ChartProps & { metric: MetricKey }) {
  const cfg = METRICS[metric];
  const rows = sortByMetric(models, metric);
  const max =
    Math.max(metric === "hard" ? 1 : 0, ...rows.map((m) => cfg.get(m) ?? 0)) ||
    1;
  return (
    <section className="panel bars" aria-label={cfg.label}>
      <header className="panel-head">
        <span className="t">{cfg.label}</span>
        <span className="u">{cfg.unit}</span>
        <span className="b">{cfg.better} is better</span>
      </header>
      <ol className={`panel-body ${hot ? "hovering" : ""}`}>
        {rows.map((m) => {
          const v = cfg.get(m) ?? 0;
          const note = cfg.note(m);
          return (
            <li
              key={m.id}
              data-model={m.id}
              className={hot === m.id ? "hot" : ""}
              onMouseEnter={() => onHover(m.id)}
              onMouseLeave={() => onHover(null)}
              title={`${shortName(m)}: ${cfg.fmt(v)}${note ? ` (${note})` : ""}`}
            >
              <span className="name">
                <i className="sw" data-slot={providerSlot(m.id)} />
                {shortName(m)}
              </span>
              {/* SVG geometry preserves the zero-based bars without CSP-blocked inline styles. */}
              <svg
                className="track"
                data-slot={providerSlot(m.id)}
                viewBox="0 0 100 10"
                preserveAspectRatio="none"
                aria-hidden="true"
              >
                <rect width={(v / max) * 100} height="10" />
              </svg>
              <span className="val num">
                {cfg.fmt(v)}
                {note && <small>{note}</small>}
              </span>
            </li>
          );
        })}
      </ol>
      {cfg.candidatesOnly && rows.length < models.length && (
        <p className="omit">Claude omitted (different harness).</p>
      )}
    </section>
  );
}

export interface YAxis<T extends Model> {
  get: (m: T) => number | null | undefined;
  title: string;
  axis: string;
  point: string;
  corner: string;
}
const HARD_Y: YAxis<Model> = {
  get: METRICS.hard.get,
  title: "Hard-task pass",
  axis: "hard-task pass",
  point: "of hard tasks",
  corner: "Claude\'s pass rate, for less",
};
const TABS: MetricKey[] = ["cost", "wall", "tokens", "turns"];
const W = 1040,
  H = 440;
const M = { l: 62, r: 16, t: 26, b: 44 };
const ZERO_LANE = 46;
const ih = H - M.t - M.b;
function niceStep(raw: number) {
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  return [1, 2, 5, 10].map((k) => k * mag).find((s) => raw <= s) ?? mag * 10;
}

// The hub plot geometry, including its log-axis zero lane, Pareto frontier and
// greedy label placement. Only the rendering syntax and palette have changed.
export function Scatter<T extends Model>({
  models,
  hot,
  onHover,
  y = HARD_Y,
}: ChartProps<T> & { y?: YAxis<T> }) {
  const [metric, setMetric] = useState<MetricKey>("cost");
  const cfg = METRICS[metric];
  const pts = plottable(models, metric)
    .filter((m) => y.get(m) != null)
    .map((m) => ({
      id: m.id,
      name: shortName(m),
      slot: providerSlot(m.id),
      anchor: m.role === "anchor",
      x: cfg.get(m) ?? 0,
      y: y.get(m)!,
    }));
  const hasZero = cfg.log && pts.some((p) => !(p.x > 0));
  const x0 = M.l + (hasZero ? ZERO_LANE : 0);
  const iw = W - M.r - x0;
  const domain = (() => {
    const values = pts.map((p) => p.x).filter((v) => v > 0);
    if (!values.length) return [0, 1] as const;
    let lo = Math.min(...values),
      hi = Math.max(...values);
    if (cfg.log) {
      if (hi === lo) [lo, hi] = [lo / 2, hi * 2];
      return [lo / 1.4, hi * 1.4] as const;
    }
    return [0, hi * 1.08 || 1] as const;
  })();
  function xs(v: number) {
    const [lo, hi] = domain;
    if (cfg.log) {
      if (!(v > 0)) return M.l + ZERO_LANE / 2;
      return x0 + (iw * Math.log(v / lo)) / Math.log(hi / lo);
    }
    return x0 + (iw * (v - lo)) / (hi - lo);
  }
  const yLo = Math.max(
    0,
    Math.floor((Math.min(1, ...pts.map((p) => p.y)) - 0.02) * 20) / 20,
  );
  const yTicks: number[] = [];
  const step = 1 - yLo > 0.5 ? 0.25 : 1 - yLo > 0.25 ? 0.1 : 0.05;
  for (let v = 1; v >= yLo - 1e-9; v -= step)
    yTicks.push(Math.round(v * 100) / 100);
  const ys = (v: number) => M.t + ih * ((1 - v) / (1 - yLo || 1));
  const ticks = (() => {
    const [lo, hi] = domain;
    const out: number[] = [];
    if (cfg.log) {
      for (
        let e = Math.floor(Math.log10(lo));
        e <= Math.ceil(Math.log10(hi));
        e++
      ) {
        // With no positive points the log domain is empty; there are no ticks.
        if (!Number.isFinite(e)) break;
        for (const k of [1, 2, 5]) {
          const v = k * 10 ** e;
          if (v >= lo && v <= hi) out.push(v);
        }
      }
      return out.length > 7
        ? out.filter(
            (v) => Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9,
          )
        : out;
    }
    const step = niceStep(hi / 5);
    for (let v = 0; v <= hi; v += step) out.push(v);
    return out;
  })();
  const anchors = pts.filter((p) => p.anchor && p.x > 0);
  const bound = anchors.length
    ? {
        x: Math.min(...anchors.map((p) => p.x)),
        y: Math.min(...anchors.map((p) => p.y)),
      }
    : null;
  const frontier = paretoFrontier(pts);
  const onFrontier = new Set(frontier.map((p) => p.id));
  const legend = providersIn(pts);
  const boxes = pts.map((p) => ({
    x1: xs(p.x) - 6,
    x2: xs(p.x) + 6,
    y1: ys(p.y) - 6,
    y2: ys(p.y) + 6,
  }));
  type Label = { x: number; y: number; anchor: "start" | "end" | "middle" };
  const placed = new Map<number, Label>();
  for (const i of [...pts.keys()].sort((a, b) => pts[a]!.x - pts[b]!.x)) {
    const p = pts[i]!,
      cx = xs(p.x),
      cy = ys(p.y),
      w = p.name.length * 6.1 + 4,
      h = 12;
    const tries: (Label & { b: number[] })[] = [
      {
        x: cx + 9,
        y: cy + 3.5,
        anchor: "start",
        b: [cx + 8, cx + 8 + w, cy - 6, cy + 6],
      },
      {
        x: cx - 9,
        y: cy + 3.5,
        anchor: "end",
        b: [cx - 8 - w, cx - 8, cy - 6, cy + 6],
      },
      {
        x: cx,
        y: cy - 10,
        anchor: "middle",
        b: [cx - w / 2, cx + w / 2, cy - 20, cy - 8],
      },
      {
        x: cx,
        y: cy + 19,
        anchor: "middle",
        b: [cx - w / 2, cx + w / 2, cy + 8, cy + 8 + h],
      },
      {
        x: cx - 4,
        y: cy - 10,
        anchor: "start",
        b: [cx - 4, cx - 4 + w, cy - 20, cy - 8],
      },
      {
        x: cx - 4,
        y: cy + 19,
        anchor: "start",
        b: [cx - 4, cx - 4 + w, cy + 8, cy + 8 + h],
      },
    ];
    for (const t of tries) {
      const box = { x1: t.b[0]!, x2: t.b[1]!, y1: t.b[2]!, y2: t.b[3]! };
      if (box.x1 < M.l + 2 || box.x2 > W || box.y1 < 0 || box.y2 > H - M.b + 14)
        continue;
      if (
        boxes.some(
          (o) =>
            box.x1 < o.x2 && box.x2 > o.x1 && box.y1 < o.y2 && box.y2 > o.y1,
        )
      )
        continue;
      boxes.push(box);
      placed.set(i, t);
      break;
    }
  }
  return (
    <section className="panel scatter">
      <header className="panel-head">
        <span className="t">
          {y.title} vs {cfg.label.toLowerCase()}
        </span>
        <span className="b">top left is best</span>
      </header>
      <div className="panel-body">
        <div className="seg" role="group" aria-label="X axis metric">
          {TABS.map((key) => (
            <button
              key={key}
              type="button"
              aria-pressed={metric === key}
              onClick={() => setMetric(key)}
            >
              {METRICS[key].label}
            </button>
          ))}
        </div>
        <p className="legend">
          {legend.map((name) => (
            <span key={name}>
              <i className="sw" data-slot={providerSlot(`${name}/`)} />
              {name}
            </span>
          ))}
          <span>
            <i className="line" />
            Pareto frontier
          </span>
          <span>
            <i className="corner" />
            {y.corner}
          </span>
        </p>
        <div
          className="plot"
          role="region"
          aria-label={`${y.title} chart`}
          tabIndex={0}
        >
          <svg
            viewBox={`0 0 ${W} ${H}`}
            role="img"
            aria-label={`${y.title} against ${cfg.unit}`}
            className={hot ? "hovering" : ""}
          >
            {bound && (
              <rect
                className="zone"
                x={M.l}
                y={M.t - 9}
                width={Math.max(0, xs(bound.x) - M.l)}
                height={ys(bound.y) - M.t + 18}
              />
            )}
            {yTicks.map((t) => (
              <Fragment key={t}>
                <line
                  className="grid"
                  x1={M.l}
                  x2={W - M.r}
                  y1={ys(t)}
                  y2={ys(t)}
                />
                <text
                  className="tick"
                  x={M.l - 6}
                  y={ys(t) + 3.5}
                  textAnchor="end"
                >
                  {Math.round(t * 100)}%
                </text>
              </Fragment>
            ))}
            {ticks.map((t) => (
              <text
                key={t}
                className="tick"
                x={xs(t)}
                y={M.t + ih + 15}
                textAnchor="middle"
              >
                {(cfg.tick ?? cfg.fmt)(t)}
              </text>
            ))}
            {hasZero && (
              <>
                <text
                  className="tick"
                  x={M.l + ZERO_LANE / 2}
                  y={M.t + ih + 15}
                  textAnchor="middle"
                >
                  $0
                </text>
                <path
                  className="brk"
                  d={`M${x0 - 7} ${M.t + ih + 4} l4 -8 M${x0 - 3} ${M.t + ih + 4} l4 -8`}
                />
              </>
            )}
            <line
              className="axis"
              x1={M.l}
              x2={W - M.r}
              y1={M.t + ih}
              y2={M.t + ih}
            />
            <text
              className="axis-title"
              transform={`translate(14 ${M.t + ih / 2}) rotate(-90)`}
              textAnchor="middle"
            >
              {y.axis}
            </text>
            {frontier.length > 1 && (
              <polyline
                className="frontier"
                points={frontier.map((p) => `${xs(p.x)},${ys(p.y)}`).join(" ")}
              />
            )}
            <text
              className="axis-title"
              x={x0 + iw / 2}
              y={H - 6}
              textAnchor="middle"
            >
              {cfg.unit}
              {cfg.log ? ", log scale" : ""}
            </text>
            {pts.map((p, i) => {
              const lab = placed.get(i);
              return (
                <g
                  key={p.id}
                  data-model={p.id}
                  className={`${hot === p.id ? "hot" : ""} ${onFrontier.has(p.id) ? "pareto" : ""}`}
                  role="presentation"
                  onMouseEnter={() => onHover(p.id)}
                  onMouseLeave={() => onHover(null)}
                >
                  <title>{`${p.name}: ${cfg.fmt(p.x)}, ${Math.round(p.y * 100)}% ${y.point}`}</title>
                  <circle className="hit" cx={xs(p.x)} cy={ys(p.y)} r="12" />
                  {p.anchor ? (
                    <rect
                      className="mk"
                      data-slot={p.slot}
                      x={xs(p.x) - 5.5}
                      y={ys(p.y) - 5.5}
                      width="11"
                      height="11"
                    />
                  ) : (
                    <circle
                      className="mk"
                      data-slot={p.slot}
                      cx={xs(p.x)}
                      cy={ys(p.y)}
                      r="5.5"
                    />
                  )}
                  {lab && (
                    <text
                      className="lbl"
                      x={lab.x}
                      y={lab.y}
                      textAnchor={lab.anchor}
                    >
                      {p.name}
                    </text>
                  )}
                </g>
              );
            })}
          </svg>
        </div>
        <p className="caption">Squares: Claude. Ringed: on the frontier.</p>
      </div>
    </section>
  );
}

const LABEL = {
  pass: "passed",
  fail: "failed",
  errored: "errored",
  none: "not run",
};
export function TaskMatrix({
  models,
  tasks,
  hot,
  onHover,
}: ChartProps & { tasks: Task[] }) {
  const cols = orderedTasks(tasks);
  const tiers = ["easy", "standard", "hard"]
    .map((tier) => ({ tier, span: cols.filter((t) => t.tier === tier).length }))
    .filter((g) => g.span);
  return (
    <section className="panel matrix">
      <header className="panel-head">
        <span className="t">Per-task results</span>
        <span className="u">numbers key to the task list</span>
      </header>
      <div
        className="panel-body scroll"
        role="region"
        aria-label="Task results data"
        tabIndex={0}
      >
        <table aria-label="Per-task results" className={hot ? "hovering" : ""}>
          <thead>
            <tr className="tiers">
              <td />
              {tiers.map((g) => (
                <th key={g.tier} colSpan={g.span} scope="colgroup">
                  {g.tier}
                </th>
              ))}
              <td />
            </tr>
            <tr>
              <th className="m" scope="col">
                model
              </th>
              {cols.map((t) => (
                <th
                  key={t.id}
                  className={`c num ${t.no > 1 && cols[t.no - 2]!.tier !== t.tier ? "tier-start" : ""}`}
                  scope="col"
                  title={t.id}
                >
                  <a href={`#task-${t.no}`}>{t.no}</a>
                </th>
              ))}
              <th className="s" scope="col">
                solved
              </th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => {
              const states = cols.map((t) => cellState(m, t.id));
              return (
                <tr
                  key={m.id}
                  data-model={m.id}
                  className={hot === m.id ? "hot" : ""}
                  onMouseEnter={() => onHover(m.id)}
                  onMouseLeave={() => onHover(null)}
                >
                  <th className="m" scope="row">
                    <i className="sw" data-slot={providerSlot(m.id)} />
                    {shortName(m)}
                  </th>
                  {cols.map((t, i) => (
                    <td
                      key={t.id}
                      className={
                        i > 0 && cols[i - 1]!.tier !== t.tier
                          ? "tier-start"
                          : ""
                      }
                      title={`${shortName(m)} · ${t.id}: ${LABEL[states[i]!]}`}
                    >
                      <i
                        className={`cell ${states[i]}`}
                        role="img"
                        aria-label={LABEL[states[i]!]}
                      />
                    </td>
                  ))}
                  <td className="s num">
                    {states.filter((s) => s === "pass").length}/
                    {states.filter((s) => s === "pass" || s === "fail").length}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="key">
        <span>
          <i className="cell pass" />
          passed
        </span>
        <span>
          <i className="cell fail" />
          failed
        </span>
        <span>
          <i className="cell errored" />
          errored (not counted)
        </span>
        <span>
          <i className="cell none" />
          not run
        </span>
      </p>
    </section>
  );
}
