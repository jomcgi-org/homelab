<script>
  // Quality-vs-efficiency scatter for the LLM leaderboard, in the style of the
  // DeepSWE / Artificial-Analysis leaderboards: pass-rate on the Y-axis, an
  // efficiency metric on a REVERSED X-axis (so "most efficient" is top-right), and
  // tabs to swap the X metric. Cost and wall-time are the cloud lens (what you pay to
  // rent the model); output tokens and agent steps are the self-host lens (how long
  // your own GPU is busy, since $ and cloud latency do not transfer to local hardware).
  // A task selector switches between the per-task mean and a single task (where the
  // Y-axis collapses to pass/fail for that one task).
  let { models = [], tasks = [] } = $props();

  let metric = $state("cost"); // cost | wall | tokens | turns
  let taskSel = $state("all"); // 'all' | task id

  const shortName = (id) =>
    id.includes("/") ? id.split("/").slice(1).join("/") : id;
  const provider = (id) => (id.includes("/") ? id.split("/")[0] : id);
  const cellOf = (m, tid) => (m.tasks ?? []).find((t) => t.id === tid);

  const money = (v) => `$${v < 0.01 ? v.toFixed(4) : v.toFixed(3)}`;
  const secs = (v) => `${v.toFixed(v < 10 ? 1 : 0)}s`;
  const kfmt = (v) =>
    v >= 1000
      ? `${(v / 1000).toFixed(v >= 10000 ? 0 : 1)}k`
      : `${Math.round(v)}`;
  const round = (v) => `${v.toFixed(v < 10 ? 1 : 0)}`;

  // Each tab: the X metric. `get` reads the model-level mean; `cell` reads one task's
  // raw value; `log` picks a log axis for the wide-range metrics.
  const METRICS = {
    cost: {
      label: "Cost",
      axis: "avg cost per task",
      lens: "cloud",
      beat: "cheaper than Claude",
      log: true,
      get: (m) => m.cost_usd,
      cell: (c) => c.cost_usd,
      fmt: money,
    },
    wall: {
      label: "Wall-time",
      axis: "avg wall-time per task",
      lens: "cloud",
      beat: "faster than Claude",
      log: false,
      get: (m) => (m.mean_latency_ms ?? 0) / 1000,
      cell: (c) => c.latency_ms / 1000,
      fmt: secs,
    },
    tokens: {
      label: "Output tokens",
      axis: "avg tokens per task",
      lens: "self-host",
      beat: "leaner than Claude",
      log: true,
      get: (m) => m.mean_tokens,
      cell: (c) => c.tokens,
      fmt: kfmt,
    },
    turns: {
      label: "Agent steps",
      axis: "avg steps per task",
      lens: "self-host",
      beat: "fewer steps than Claude",
      log: false,
      get: (m) => m.mean_turns,
      cell: (c) => c.turns,
      fmt: round,
    },
  };
  const MK = ["cost", "wall", "tokens", "turns"];
  const cfg = $derived(METRICS[metric]);
  const isAll = $derived(taskSel === "all");

  // Provider palette (chart-local: the design system doesn't carry 6 distinct hues).
  const PROV = {
    anthropic: "#ff7169",
    qwen: "#29a187",
    google: "#3b82c4",
    deepseek: "#7c5cff",
    "z-ai": "#e0851e",
    mistralai: "#d94f9a",
  };
  const provColor = (id) => PROV[provider(id)] ?? "#6b6658";

  const points = $derived.by(() => {
    const pts = [];
    for (const m of models) {
      let x, y;
      if (isAll) {
        // Y is HARD-task pass, the discriminator the board ranks by and the axis on
        // which "matching frontier capability" means reaching the Claude anchors' level.
        // Overall pass_rate blurs the good models together (the floor tasks saturate) and
        // even sinks the best-value pick below pricier peers over one floor miss.
        y = m.hard_n ? m.hard_pass / m.hard_n : 0;
        x = cfg.get(m);
      } else {
        const c = cellOf(m, taskSel);
        if (!c) continue;
        y = c.passed ? 1 : 0;
        x = cfg.cell(c);
      }
      if (!(x > 0)) continue; // metric must be positive (log axis + a real run)
      pts.push({
        id: m.id,
        name: m.name ?? shortName(m.id),
        anchor: m.role === "anchor",
        x,
        y,
      });
    }
    return pts;
  });

  // Budget-zone boundary: the most efficient Claude anchor on the current metric.
  // Everything more efficient than it (to the right, since X is reversed) is a
  // candidate to replace Claude, which is the whole point of the benchmark.
  const anchorBound = $derived.by(() => {
    const a = points.filter((p) => p.anchor).map((p) => p.x);
    return a.length ? Math.min(...a) : null;
  });
  const providersShown = $derived([
    ...new Set(points.map((p) => provider(p.id))),
  ]);

  // ---- geometry ----
  const W = 760;
  const H = 460;
  const M = { l: 46, r: 150, t: 24, b: 48 };
  const iw = W - M.l - M.r;
  const ih = H - M.t - M.b;

  const xdomain = $derived.by(() => {
    const xs = points.map((p) => p.x);
    let lo = Math.min(...xs);
    let hi = Math.max(...xs);
    if (!(hi > lo)) {
      lo *= 0.9;
      hi *= 1.1;
    }
    if (cfg.log) return [lo * 0.85, hi * 1.15];
    return [0, hi * 1.08]; // linear metrics anchor the efficient end at 0 (right)
  });

  // X is REVERSED: the max (least efficient) sits on the left, min on the right.
  const xScale = (v) => {
    const [lo, hi] = xdomain;
    if (cfg.log) {
      const vv = Math.max(v, lo);
      return (
        M.l +
        (iw * (Math.log(hi) - Math.log(vv))) / (Math.log(hi) - Math.log(lo))
      );
    }
    return M.l + (iw * (hi - v)) / (hi - lo);
  };
  // Y = pass rate, 0..1, not inverted (100% at top).
  const yScale = (v) => M.t + ih * (1 - v);

  // Pareto frontier: max pass, min metric. Non-dominated set, drawn as the top-right
  // envelope (screen-left = expensive, screen-right = cheap).
  const frontier = $derived.by(() => {
    const nd = points.filter(
      (p) =>
        !points.some(
          (q) =>
            q !== p && q.y >= p.y && q.x <= p.x && (q.y > p.y || q.x < p.x),
        ),
    );
    return nd.sort((a, b) => b.x - a.x); // expensive -> cheap == screen left -> right
  });
  const frontierIds = $derived(new Set(frontier.map((p) => p.id)));

  // ---- labels: place to the side away from the plot edge, then de-collide vertically
  // within each side so the saturated 100% band does not stack labels on top of each other.
  const laidOut = $derived.by(() => {
    const rightThreshold = M.l + iw * 0.68;
    const rows = points.map((p) => {
      const cx = xScale(p.x);
      const cy = yScale(p.y);
      const leftSide = cx > rightThreshold; // near right edge -> label to the left
      // Only label the spread-out, decision-relevant points: the frontier, the Claude
      // anchors, and any model below a perfect pass rate. The dense band of 100%-pass
      // models would collide into mush if all labelled; the ranked table below (and the
      // hover title) identifies them instead.
      const show = frontierIds.has(p.id) || p.anchor || p.y < 0.999;
      return { ...p, cx, cy, leftSide, ly: cy, show };
    });
    for (const side of [true, false]) {
      const grp = rows
        .filter((r) => r.show && r.leftSide === side)
        .sort((a, b) => a.ly - b.ly);
      for (let i = 1; i < grp.length; i++) {
        if (grp[i].ly - grp[i - 1].ly < 13) grp[i].ly = grp[i - 1].ly + 13;
      }
    }
    return rows;
  });

  const yticks = [0, 0.2, 0.4, 0.6, 0.8, 1];
  const xticks = $derived.by(() => {
    const [lo, hi] = xdomain;
    const out = [];
    for (let i = 0; i < 5; i++) {
      out.push(
        cfg.log
          ? Math.exp(Math.log(lo) + ((Math.log(hi) - Math.log(lo)) * i) / 4)
          : lo + ((hi - lo) * i) / 4,
      );
    }
    return out;
  });
</script>

<div class="scatter">
  <div class="controls">
    <div class="tabs" role="group" aria-label="Metric">
      {#each MK as k}
        <button class:on={metric === k} onclick={() => (metric = k)}
          >{METRICS[k].label}</button
        >
      {/each}
    </div>
    <label class="task">
      <span>Task</span>
      <select bind:value={taskSel}>
        <option value="all">All tasks (mean)</option>
        {#each tasks as t}
          <option value={t.id}>{t.id}</option>
        {/each}
      </select>
    </label>
    <span class="lens-tag">{cfg.lens} lens</span>
  </div>

  {#if !points.length}
    <p class="empty">No data to plot for this view.</p>
  {:else}
    <svg
      viewBox="0 0 {W} {H}"
      role="img"
      aria-label="Hard-task pass versus {cfg.axis}"
    >
      <text class="ylab" x={M.l} y={M.t - 8}
        >{isAll ? "hard-task pass" : "pass"}</text
      >
      <text class="eff" x={M.l + iw} y={M.t - 8} text-anchor="end"
        >most efficient ↗</text
      >

      {#each yticks as tv}
        <line
          class="grid"
          x1={M.l}
          y1={yScale(tv)}
          x2={M.l + iw}
          y2={yScale(tv)}
        />
        <text class="tick" x={M.l - 8} y={yScale(tv) + 3} text-anchor="end"
          >{Math.round(tv * 100)}%</text
        >
      {/each}
      {#each xticks as tv}
        <text class="tick" x={xScale(tv)} y={M.t + ih + 16} text-anchor="middle"
          >{cfg.fmt(tv)}</text
        >
      {/each}
      <text class="axis-title" x={M.l + iw / 2} y={H - 8} text-anchor="middle"
        >{cfg.axis}{cfg.log ? " (log)" : ""}</text
      >

      <!-- budget zone: right of the most efficient Claude anchor -->
      {#if anchorBound != null && xScale(anchorBound) < M.l + iw - 4}
        <rect
          class="zone"
          x={xScale(anchorBound)}
          y={M.t}
          width={M.l + iw - xScale(anchorBound)}
          height={ih}
        />
        <line
          class="zone-edge"
          x1={xScale(anchorBound)}
          y1={M.t}
          x2={xScale(anchorBound)}
          y2={M.t + ih}
        />
        <text class="zone-lab" x={xScale(anchorBound) + 6} y={M.t + ih - 8}
          >{cfg.beat}</text
        >
      {/if}

      {#if frontier.length > 1}
        <polyline
          class="frontier"
          points={frontier
            .map((p) => `${xScale(p.x)},${yScale(p.y)}`)
            .join(" ")}
        />
      {/if}

      {#each laidOut as p (p.id)}
        <g class="pt" class:on-frontier={frontierIds.has(p.id)}>
          <title>{p.name}: {cfg.fmt(p.x)}, {Math.round(p.y * 100)}% pass</title>
          {#if p.show}
            <line
              class="lead"
              x1={p.cx}
              y1={p.cy}
              x2={p.leftSide ? p.cx - 10 : p.cx + 10}
              y2={p.ly}
            />
          {/if}
          <circle
            class="mark"
            cx={p.cx}
            cy={p.cy}
            r={p.anchor ? 6.5 : 5.5}
            style="fill:{provColor(p.id)}"
            class:anchor={p.anchor}
          />
          {#if p.show}
            <text
              class="lbl"
              x={p.leftSide ? p.cx - 13 : p.cx + 13}
              y={p.ly + 3}
              text-anchor={p.leftSide ? "end" : "start"}
              style="fill:{provColor(p.id)}"
              >{p.name}{p.anchor ? " (anchor)" : ""}</text
            >
          {/if}
        </g>
      {/each}
    </svg>

    <div class="cap">
      {cfg.label} · {isAll ? "hard-task pass, mean over all tasks" : taskSel} · match
      the frontier (top), beat its {cfg.label.toLowerCase()} ceiling (right)
    </div>
    <div class="key">
      {#each providersShown as pv}
        <span class="k"
          ><i class="sw" style="background:{PROV[pv] ?? '#6b6658'}"
          ></i>{pv}</span
        >
      {/each}
      <span class="k frontier-k"><i class="sw-line"></i>frontier</span>
    </div>
  {/if}
</div>

<style>
  .scatter {
    padding: 12px 14px 14px;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 16px;
    align-items: center;
    margin-bottom: 10px;
  }
  .tabs {
    display: inline-flex;
    border: 2px solid var(--ink);
  }
  .tabs button {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    padding: 5px 11px;
    background: var(--paper);
    color: var(--ink);
    border: none;
    cursor: pointer;
  }
  .tabs button + button {
    border-left: 2px solid var(--ink);
  }
  .tabs button.on {
    background: var(--accent);
  }
  .task {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
  }
  .task select {
    font-family: var(--mono);
    font-size: 12px;
    padding: 4px 8px;
    border: 2px solid var(--ink);
    background: var(--paper);
    color: var(--ink);
  }
  .lens-tag {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  svg {
    width: 100%;
    height: auto;
    display: block;
  }
  .grid {
    stroke: var(--rule);
    stroke-width: 1;
  }
  .tick {
    font-family: var(--mono);
    font-size: 10px;
    fill: var(--ink-3);
  }
  .ylab,
  .eff {
    font-family: var(--mono);
    font-size: 11px;
    fill: var(--ink-3);
  }
  .eff {
    font-style: italic;
  }
  .axis-title {
    font-family: var(--mono);
    font-size: 11px;
    fill: var(--ink-2);
  }
  .zone {
    fill: var(--teal);
    opacity: 0.07;
  }
  .zone-edge {
    stroke: var(--teal);
    stroke-width: 1.5;
    stroke-dasharray: 3 3;
  }
  .zone-lab {
    font-family: var(--mono);
    font-size: 10px;
    fill: var(--teal);
    font-weight: 700;
  }
  .frontier {
    fill: none;
    stroke: var(--ink-3);
    stroke-width: 1.5;
    stroke-dasharray: 4 3;
  }
  .mark {
    stroke: var(--paper);
    stroke-width: 1.5;
  }
  .mark.anchor {
    stroke: var(--ink);
    stroke-width: 2;
  }
  .pt.on-frontier .mark {
    stroke: var(--ink);
    stroke-width: 2;
  }
  .lead {
    stroke: var(--rule-2);
    stroke-width: 1;
  }
  .lbl {
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 700;
    paint-order: stroke;
    stroke: var(--paper);
    stroke-width: 3;
  }
  .cap {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
    margin-top: 8px;
  }
  .key {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 14px;
    margin-top: 8px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--ink-3);
  }
  .k {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .sw {
    width: 11px;
    height: 11px;
    border: 1px solid var(--ink);
    display: inline-block;
  }
  .sw-line {
    width: 16px;
    height: 0;
    border-top: 1.5px dashed var(--ink-3);
    display: inline-block;
  }
  .empty {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
    padding: 24px 0;
  }
</style>
