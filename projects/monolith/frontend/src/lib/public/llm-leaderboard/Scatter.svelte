<script>
  // Bubble scatter for the LLM leaderboard, with a cloud/self-host lens toggle and a
  // task selector. Two ideas drive it:
  //   1. Lenses are different plots, not relabels. Cloud is a cost x speed plane (you
  //      rent the model); self-host is a tokens x turns plane (you own the GPU, so $
  //      and cloud latency do not transfer, but tokens = GPU-seconds still do).
  //   2. Tasks vary ~5x in size, so aggregating raw is dominated by the big ones. Pick
  //      a single task and everything is raw + like-for-like; pick "All tasks" and each
  //      axis becomes a geomean of per-task ratios to that task's cross-model median
  //      ("x vs task-typical"), so an easy 6s task counts as much as a greenfield build.
  // Direction convention in both lenses: up-and-left = better (cheaper/fewer on the
  // log-x, faster/fewer-turns at the top via inverted-y), so the frontier is the
  // top-left envelope.
  let { models = [], tasks = [] } = $props();

  let lens = $state("cloud"); // 'cloud' | 'selfhost'
  let taskSel = $state("all"); // 'all' | task id

  const isAnchor = (m) => m.role === "anchor";
  const shortName = (id) => (id.includes("/") ? id.split("/").slice(1).join("/") : id);
  const cellOf = (m, tid) => (m.tasks ?? []).find((t) => t.id === tid);

  const geomean = (vals) => {
    const v = vals.filter((x) => x > 0);
    if (!v.length) return 0;
    return Math.exp(v.reduce((a, b) => a + Math.log(b), 0) / v.length);
  };
  const median = (vals) => {
    const v = vals.filter((x) => x > 0).sort((a, b) => a - b);
    if (!v.length) return 0;
    const n = v.length;
    return n % 2 ? v[(n - 1) / 2] : (v[n / 2 - 1] + v[n / 2]) / 2;
  };

  // Per-task baseline = median across models over the runs that PASSED (so the baseline
  // is a typical successful run, not skewed by a model that bailed early with 0 tokens).
  const baseline = $derived.by(() => {
    const b = {};
    for (const t of tasks) {
      const cells = models.map((m) => cellOf(m, t.id)).filter((c) => c && c.passed);
      b[t.id] = {
        tokens: median(cells.map((c) => c.tokens)),
        latency_ms: median(cells.map((c) => c.latency_ms)),
        turns: median(cells.map((c) => c.turns)),
        cost_usd: median(cells.map((c) => c.cost_usd)),
      };
    }
    return b;
  });

  // Geomean of (this model's passed value / task baseline) across tasks.
  const indexOf = (m, metric) => {
    const ratios = [];
    for (const t of tasks) {
      const c = cellOf(m, t.id);
      const base = baseline[t.id]?.[metric];
      if (c && c.passed && c[metric] > 0 && base > 0) ratios.push(c[metric] / base);
    }
    return geomean(ratios);
  };

  const isAll = $derived(taskSel === "all");
  const hasSize = $derived(lens === "cloud"); // bubble size = tokens/effort, cloud only

  // Build the plotted points for the current lens + task selection.
  const points = $derived.by(() => {
    const pts = [];
    for (const m of models) {
      let x, y, size, pass;
      if (isAll) {
        pass = m.pass_rate ?? 0;
        if (lens === "cloud") {
          x = indexOf(m, "cost_usd");
          y = indexOf(m, "latency_ms");
          size = indexOf(m, "tokens");
        } else {
          x = indexOf(m, "tokens");
          y = indexOf(m, "turns");
          size = 1;
        }
      } else {
        const c = cellOf(m, taskSel);
        if (!c) continue;
        pass = c.passed ? 1 : 0;
        if (lens === "cloud") {
          x = c.cost_usd;
          y = c.latency_ms / 1000;
          size = c.tokens;
        } else {
          x = c.tokens;
          y = c.turns;
          size = 1;
        }
      }
      if (!(x > 0) || !(y >= 0)) continue; // log-x needs positive x
      pts.push({ id: m.id, name: shortName(m.id), anchor: isAnchor(m), x, y, size, pass });
    }
    return pts;
  });

  // ---- geometry ----
  const W = 720;
  const H = 430;
  const M = { l: 58, r: 128, t: 20, b: 50 };
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
    return [lo, hi];
  });
  const ydomain = $derived.by(() => {
    const ys = points.map((p) => p.y);
    let lo = Math.min(...ys);
    let hi = Math.max(...ys);
    const pad = hi > lo ? (hi - lo) * 0.1 : Math.max(hi * 0.2, 1);
    return [Math.max(0, lo - pad), hi + pad];
  });

  const xScale = (v) => {
    const [lo, hi] = xdomain;
    return M.l + (iw * (Math.log(v) - Math.log(lo))) / (Math.log(hi) - Math.log(lo));
  };
  // Inverted: small y (faster / fewer turns) sits at the top.
  const yScale = (v) => {
    const [lo, hi] = ydomain;
    return M.t + (ih * (v - lo)) / (hi - lo);
  };
  const rScale = (s) => {
    if (!hasSize) return 7;
    const ss = points.map((p) => p.size);
    const lo = Math.min(...ss);
    const hi = Math.max(...ss);
    const norm = hi > lo ? (s - lo) / (hi - lo) : 0.5;
    return 5 + 13 * Math.sqrt(Math.max(0, norm));
  };

  const passClass = (p) => (p >= 0.999 ? "pass" : p > 0 ? "partial" : "fail");

  // Frontier over reliable points only (a cheap+fast run that FAILED is not a good
  // tradeoff). Minimise x and y; keep the non-dominated set, drawn left-to-right.
  const frontier = $derived.by(() => {
    const cand = points.filter((p) => p.pass >= (isAll ? 0.999 : 1));
    const nd = cand.filter(
      (p) => !cand.some((q) => q !== p && q.x <= p.x && q.y <= p.y && (q.x < p.x || q.y < p.y)),
    );
    return nd.sort((a, b) => a.x - b.x);
  });
  const frontierIds = $derived(new Set(frontier.map((p) => p.id)));
  const labelled = $derived(points.filter((p) => p.anchor || frontierIds.has(p.id)));

  // ---- ticks + formatting ----
  const logTicks = (dom, n) => {
    const [lo, hi] = dom;
    const out = [];
    for (let i = 0; i < n; i++) out.push(Math.exp(Math.log(lo) + ((Math.log(hi) - Math.log(lo)) * i) / (n - 1)));
    return out;
  };
  const linTicks = (dom, n) => {
    const [lo, hi] = dom;
    const out = [];
    for (let i = 0; i < n; i++) out.push(lo + ((hi - lo) * i) / (n - 1));
    return out;
  };
  const xticks = $derived(logTicks(xdomain, 4));
  const yticks = $derived(linTicks(ydomain, 4));

  const kfmt = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : `${Math.round(n)}`);
  const fmtX = (v) => {
    if (isAll) return `${v.toFixed(1)}×`;
    return lens === "cloud" ? `$${v.toFixed(v < 0.01 ? 4 : 3)}` : kfmt(v);
  };
  const fmtY = (v) => {
    if (isAll) return `${v.toFixed(1)}×`;
    return lens === "cloud" ? `${v.toFixed(1)}s` : `${Math.round(v)}`;
  };

  const xTitle = $derived(
    lens === "cloud"
      ? isAll
        ? "cost, × vs task-typical (log)"
        : "cost per task, $ (log)"
      : isAll
        ? "tokens, × vs task-typical (log)"
        : "tokens (log)",
  );
  const yTitle = $derived(
    lens === "cloud"
      ? isAll
        ? "wall-time, × vs task-typical"
        : "wall-time, s"
      : isAll
        ? "turns, × vs task-typical"
        : "turns",
  );
  const caption = $derived(
    `${lens === "cloud" ? "Cloud lens" : "Self-host lens"} · ${
      isAll ? "all tasks, normalized" : taskSel
    } · better = top-left`,
  );

  const diamond = (cx, cy, r) => `M ${cx} ${cy - r} L ${cx + r} ${cy} L ${cx} ${cy + r} L ${cx - r} ${cy} Z`;
  const labelAnchor = (x) => (x > M.l + iw * 0.72 ? "end" : "start");
  const labelDx = (x, r) => (x > M.l + iw * 0.72 ? -(r + 5) : r + 5);
</script>

<div class="scatter">
  <div class="controls">
    <div class="lens" role="group" aria-label="Lens">
      <button class:on={lens === "cloud"} onclick={() => (lens = "cloud")}>Cloud</button>
      <button class:on={lens === "selfhost"} onclick={() => (lens = "selfhost")}>Self-host</button>
    </div>
    <label class="task">
      <span>Task</span>
      <select bind:value={taskSel}>
        <option value="all">All tasks (normalized)</option>
        {#each tasks as t}
          <option value={t.id}>{t.id}</option>
        {/each}
      </select>
    </label>
  </div>

  {#if !points.length}
    <p class="empty">No per-task data to plot for this view.</p>
  {:else}
  <svg viewBox="0 0 {W} {H}" role="img" aria-label="Model {lens} scatter, {caption}">
    <!-- axes -->
    <line class="axis" x1={M.l} y1={M.t + ih} x2={M.l + iw} y2={M.t + ih} />
    <line class="axis" x1={M.l} y1={M.t} x2={M.l} y2={M.t + ih} />
    {#each xticks as tv}
      <text class="tick" x={xScale(tv)} y={M.t + ih + 16} text-anchor="middle">{fmtX(tv)}</text>
    {/each}
    {#each yticks as tv}
      <line class="grid" x1={M.l} y1={yScale(tv)} x2={M.l + iw} y2={yScale(tv)} />
      <text class="tick" x={M.l - 8} y={yScale(tv) + 3} text-anchor="end">{fmtY(tv)}</text>
    {/each}
    <text class="axis-title" x={M.l + iw / 2} y={H - 6} text-anchor="middle">{xTitle}</text>
    <text
      class="axis-title"
      x={14}
      y={M.t + ih / 2}
      text-anchor="middle"
      transform="rotate(-90 14 {M.t + ih / 2})">{yTitle}</text
    >

    <!-- pareto frontier line -->
    {#if frontier.length > 1}
      <polyline class="frontier" points={frontier.map((p) => `${xScale(p.x)},${yScale(p.y)}`).join(" ")} />
    {/if}

    <!-- points -->
    {#each points as p (p.id)}
      {@const cx = xScale(p.x)}
      {@const cy = yScale(p.y)}
      {@const r = rScale(p.size)}
      <g class="pt {passClass(p.pass)}" class:on-frontier={frontierIds.has(p.id)}>
        <title>{p.name}: {fmtX(p.x)}, {fmtY(p.y)}</title>
        {#if p.anchor}
          <path class="mark anchor" d={diamond(cx, cy, r)} />
        {:else}
          <circle class="mark" {cx} {cy} {r} />
        {/if}
      </g>
    {/each}

    <!-- labels for frontier + anchors only, to avoid clutter -->
    {#each labelled as p (p.id)}
      {@const cx = xScale(p.x)}
      {@const cy = yScale(p.y)}
      {@const r = rScale(p.size)}
      <text class="lbl" x={cx + labelDx(cx, r)} y={cy + 3} text-anchor={labelAnchor(cx)}>{p.name}</text>
    {/each}
  </svg>

  <div class="cap">{caption}</div>
  <div class="key">
    <span class="k"><i class="sw pass"></i>100% pass</span>
    <span class="k"><i class="sw partial"></i>partial</span>
    <span class="k"><i class="sw fail"></i>fail</span>
    <span class="k"><i class="sw anchor-sw"></i>Claude anchor</span>
    <span class="k frontier-k"><i class="sw-line"></i>frontier</span>
    {#if hasSize}<span class="k">bubble = tokens (effort)</span>{/if}
  </div>
  {/if}
</div>

<style>
  .scatter {
    padding: 12px 14px 14px;
  }
  .empty {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-3);
    padding: 24px 0;
  }
  .controls {
    display: flex;
    flex-wrap: wrap;
    gap: 12px 18px;
    align-items: center;
    margin-bottom: 10px;
  }
  .lens {
    display: inline-flex;
    border: 2px solid var(--ink);
  }
  .lens button {
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    padding: 5px 12px;
    background: var(--paper);
    color: var(--ink);
    border: none;
    cursor: pointer;
  }
  .lens button + button {
    border-left: 2px solid var(--ink);
  }
  .lens button.on {
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

  svg {
    width: 100%;
    height: auto;
    display: block;
  }
  .axis {
    stroke: var(--ink);
    stroke-width: 2;
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
  .axis-title {
    font-family: var(--mono);
    font-size: 11px;
    fill: var(--ink-2);
  }
  .frontier {
    fill: none;
    stroke: var(--ink);
    stroke-width: 1.5;
    stroke-dasharray: 4 3;
  }
  .mark {
    stroke: var(--ink);
    stroke-width: 1.5;
  }
  .pt.pass .mark {
    fill: var(--teal);
  }
  .pt.partial .mark {
    fill: var(--accent);
  }
  .pt.fail .mark {
    fill: var(--coral);
  }
  .mark.anchor {
    stroke-width: 2.5;
  }
  .pt.on-frontier .mark {
    stroke-width: 3;
  }
  .lbl {
    font-family: var(--mono);
    font-size: 10.5px;
    font-weight: 700;
    fill: var(--ink);
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
    border: 1.5px solid var(--ink);
    display: inline-block;
  }
  .sw.pass {
    background: var(--teal);
  }
  .sw.partial {
    background: var(--accent);
  }
  .sw.fail {
    background: var(--coral);
  }
  .sw.anchor-sw {
    background: var(--paper);
    transform: rotate(45deg);
  }
  .sw-line {
    width: 16px;
    height: 0;
    border-top: 1.5px dashed var(--ink);
    display: inline-block;
  }
</style>
