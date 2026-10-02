<script>
  // Capability against what it costs to get it: hard-task pass on Y, a chosen
  // efficiency metric on X, lower to the left, so the place to be is the top
  // left. Two overlays carry the argument, both after Artificial Analysis:
  // the Pareto frontier (dotted), the models nothing else beats on both axes
  // at once; and the attractive corner (green), at least Claude's pass rate
  // for less than Claude's cost on this axis, which is the question the
  // benchmark exists to ask.
  //
  // Cost and tokens span two orders of magnitude, so they plot on a log axis.
  // A self-hosted model costs $0, which a log axis cannot place, so $0 gets its
  // own lane left of an axis break rather than being dropped from the plot.
  import {
    METRICS,
    paretoFrontier,
    plottable,
    providerSlot,
    providersIn,
    shortName,
  } from "./model.js";

  let { models = [], hot = null, onhover = () => {} } = $props();

  const TABS = ["cost", "wall", "tokens", "turns"];
  let metric = $state("cost");
  const cfg = $derived(METRICS[metric]);

  const W = 1040;
  const H = 440;
  const M = { l: 62, r: 16, t: 26, b: 44 };
  const ZERO_LANE = 46; // px reserved left of the break for $0 points
  const ih = H - M.t - M.b;

  const pts = $derived(
    plottable(models, metric).map((m) => ({
      id: m.id,
      name: shortName(m),
      slot: providerSlot(m.id),
      anchor: m.role === "anchor",
      x: cfg.get(m) ?? 0,
      y: METRICS.hard.get(m),
    })),
  );
  const hasZero = $derived(cfg.log && pts.some((p) => !(p.x > 0)));
  const x0 = $derived(M.l + (hasZero ? ZERO_LANE : 0));
  const iw = $derived(W - M.r - x0);

  const domain = $derived.by(() => {
    const xs = pts.map((p) => p.x).filter((v) => v > 0);
    if (!xs.length) return [0, 1];
    let lo = Math.min(...xs);
    let hi = Math.max(...xs);
    if (cfg.log) {
      if (hi === lo) [lo, hi] = [lo / 2, hi * 2];
      return [lo / 1.4, hi * 1.4];
    }
    return [0, hi * 1.08 || 1];
  });

  function xs(v) {
    const [lo, hi] = domain;
    if (cfg.log) {
      if (!(v > 0)) return M.l + ZERO_LANE / 2;
      return x0 + (iw * Math.log(v / lo)) / Math.log(hi / lo);
    }
    return x0 + (iw * (v - lo)) / (hi - lo);
  }
  // Y zooms to the data in 10% steps: the selection usually sits between 80
  // and 100%, and a 0..100% axis spends most of the plot on empty space.
  const yLo = $derived(
    Math.max(
      0,
      Math.floor((Math.min(1, ...pts.map((p) => p.y)) - 0.02) * 20) / 20,
    ),
  );
  const yTicks = $derived.by(() => {
    const step = 1 - yLo > 0.5 ? 0.25 : 1 - yLo > 0.25 ? 0.1 : 0.05;
    const out = [];
    for (let v = 1; v >= yLo - 1e-9; v -= step)
      out.push(Math.round(v * 100) / 100);
    return out;
  });
  const ys = (v) => M.t + ih * ((1 - v) / (1 - yLo || 1));

  const ticks = $derived.by(() => {
    const [lo, hi] = domain;
    if (cfg.log) {
      const out = [];
      for (
        let e = Math.floor(Math.log10(lo));
        e <= Math.ceil(Math.log10(hi));
        e++
      )
        for (const k of [1, 2, 5]) {
          const v = k * 10 ** e;
          if (v >= lo && v <= hi) out.push(v);
        }
      // Too many ticks: keep the powers of ten only.
      const decade = (v) =>
        Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9;
      return out.length > 7 ? out.filter(decade) : out;
    }
    const step = niceStep(hi / 5);
    const out = [];
    for (let v = 0; v <= hi; v += step) out.push(v);
    return out;
  });

  function niceStep(raw) {
    const mag = 10 ** Math.floor(Math.log10(raw || 1));
    return [1, 2, 5, 10].map((k) => k * mag).find((s) => raw <= s) ?? mag * 10;
  }

  // The attractive corner: left of the cheapest anchor, at or above the
  // weakest anchor's pass rate.
  const bound = $derived.by(() => {
    const a = pts.filter((p) => p.anchor && p.x > 0);
    if (!a.length) return null;
    return {
      x: Math.min(...a.map((p) => p.x)),
      y: Math.min(...a.map((p) => p.y)),
    };
  });

  // Pareto frontier: no other point has at least the pass rate for at most
  // the cost with one of them strictly better. Sorted left to right it steps
  // up, so the dotted line reads as "what the next dollar buys".
  const frontier = $derived(paretoFrontier(pts));
  const onFrontier = $derived(new Set(frontier.map((p) => p.id)));

  const legend = $derived(providersIn(pts));

  // Greedy label placement: right of the mark, then left, then above, then
  // below; the first spot that hits no placed label or mark wins. A label
  // that finds no room is dropped to the hover title and the table, rather
  // than printed on top of another one.
  const placed = $derived.by(() => {
    const boxes = pts.map((p) => {
      const cx = xs(p.x);
      const cy = ys(p.y);
      return { x1: cx - 6, x2: cx + 6, y1: cy - 6, y2: cy + 6 };
    });
    const hit = (b) =>
      boxes.some(
        (o) => b.x1 < o.x2 && b.x2 > o.x1 && b.y1 < o.y2 && b.y2 > o.y1,
      );
    const order = [...pts.keys()].sort((a, b) => pts[a].x - pts[b].x);
    const out = new Array(pts.length).fill(null);
    for (const i of order) {
      const p = pts[i];
      const cx = xs(p.x);
      const cy = ys(p.y);
      const w = p.name.length * 6.1 + 4;
      const h = 12;
      const tries = [
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
        const box = { x1: t.b[0], x2: t.b[1], y1: t.b[2], y2: t.b[3] };
        if (
          box.x1 < M.l - 30 ||
          box.x2 > W ||
          box.y1 < 0 ||
          box.y2 > H - M.b + 14
        )
          continue;
        if (hit(box)) continue;
        boxes.push(box);
        out[i] = t;
        break;
      }
    }
    return out;
  });
</script>

<section class="panel scatter">
  <header class="panel-head">
    <span class="t">Hard-task pass vs {cfg.label.toLowerCase()}</span>
    <span class="b">top left is best</span>
  </header>
  <div class="panel-body">
    <div class="seg" role="group" aria-label="X axis metric">
      {#each TABS as key}
        <button
          type="button"
          aria-pressed={metric === key}
          onclick={() => (metric = key)}>{METRICS[key].label}</button
        >
      {/each}
    </div>

    <p class="legend">
      {#each legend as name}
        <span
          ><i class="sw" data-slot={providerSlot(`${name}/`)}></i>{name}</span
        >
      {/each}
      <span><i class="line"></i>Pareto frontier</span>
      <span><i class="corner"></i>Claude's pass rate, for less</span>
    </p>

    <div class="plot">
      <svg
        viewBox="0 0 {W} {H}"
        role="img"
        aria-label={`Hard-task pass against ${cfg.unit}`}
        class:hovering={hot}
      >
        {#if bound}
          <!-- Padded by 9px so a corner that collapses to the 100% line, as it
               does while the anchors solve every hard task, still reads as a
               band around that line rather than vanishing. -->
          <rect
            class="zone"
            x={M.l}
            y={M.t - 9}
            width={Math.max(0, xs(bound.x) - M.l)}
            height={ys(bound.y) - M.t + 18}
          />
        {/if}

        {#each yTicks as t}
          <line class="grid" x1={M.l} x2={W - M.r} y1={ys(t)} y2={ys(t)} />
          <text class="tick" x={M.l - 6} y={ys(t) + 3.5} text-anchor="end"
            >{Math.round(t * 100)}%</text
          >
        {/each}

        {#each ticks as t}
          <text class="tick" x={xs(t)} y={M.t + ih + 15} text-anchor="middle"
            >{(cfg.tick ?? cfg.fmt)(t)}</text
          >
        {/each}
        {#if hasZero}
          <text
            class="tick"
            x={M.l + ZERO_LANE / 2}
            y={M.t + ih + 15}
            text-anchor="middle">$0</text
          >
          <!-- Axis break between the $0 lane and the log scale. -->
          <path
            class="brk"
            d={`M${x0 - 7} ${M.t + ih + 4} l4 -8 M${x0 - 3} ${M.t + ih + 4} l4 -8`}
          />
        {/if}
        <line class="axis" x1={M.l} x2={W - M.r} y1={M.t + ih} y2={M.t + ih} />
        <text
          class="axis-title"
          transform={`translate(14 ${M.t + ih / 2}) rotate(-90)`}
          text-anchor="middle">hard-task pass</text
        >
        {#if frontier.length > 1}
          <polyline
            class="frontier"
            points={frontier.map((p) => `${xs(p.x)},${ys(p.y)}`).join(" ")}
          />
        {/if}
        <text class="axis-title" x={x0 + iw / 2} y={H - 6} text-anchor="middle"
          >{cfg.unit}{cfg.log ? ", log scale" : ""}</text
        >

        {#each pts as p, i (p.id)}
          {@const lab = placed[i]}
          <g
            data-model={p.id}
            class:hot={hot === p.id}
            class:pareto={onFrontier.has(p.id)}
            role="presentation"
            onmouseenter={() => onhover(p.id)}
            onmouseleave={() => onhover(null)}
          >
            <title
              >{p.name}: {cfg.fmt(p.x)}, {Math.round(p.y * 100)}% of hard tasks</title
            >
            <circle class="hit" cx={xs(p.x)} cy={ys(p.y)} r="12" />
            {#if p.anchor}
              <rect
                class="mk"
                data-slot={p.slot}
                x={xs(p.x) - 5.5}
                y={ys(p.y) - 5.5}
                width="11"
                height="11"
              />
            {:else}
              <circle
                class="mk"
                data-slot={p.slot}
                cx={xs(p.x)}
                cy={ys(p.y)}
                r="5.5"
              />
            {/if}
            {#if lab}
              <text class="lbl" x={lab.x} y={lab.y} text-anchor={lab.anchor}
                >{p.name}</text
              >
            {/if}
          </g>
        {/each}
      </svg>
    </div>
    <p class="caption">Squares: Claude. Ringed: on the frontier.</p>
  </div>
</section>

<style>
  /* Below ~640px the labels would shrink past reading size, so the plot
     keeps a floor width and scrolls inside its panel instead. */
  .plot {
    overflow-x: auto;
  }

  svg {
    display: block;
    width: 100%;
    min-width: 640px;
    height: auto;
    margin-top: 0.8em;
    overflow: visible;
    font-family: var(--font-code);
  }

  .grid {
    stroke: var(--line);
  }

  .axis {
    stroke: var(--ink);
  }

  .brk {
    fill: none;
    stroke: var(--ink);
  }

  .tick {
    fill: var(--ink-2);
    font-size: 10.5px;
  }

  .axis-title {
    fill: var(--ink-2);
    font-size: 11px;
  }

  /* The attractive corner borrows Artificial Analysis's green, mixed from
     --ok so it follows the scheme: a tint on the sheet, never a fill that
     competes with the marks. */
  .zone {
    fill: color-mix(in srgb, var(--ok) 14%, var(--sheet));
  }

  .frontier {
    fill: none;
    stroke: var(--ink);
    stroke-dasharray: 1.5 4;
    stroke-linecap: round;
    stroke-width: 1.6;
  }

  .pareto .mk {
    stroke: var(--ink);
    stroke-width: 1.5;
  }

  .legend {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35em 1.2em;
    margin: 0.8em 0 0;
    color: var(--ink-2);
    font-family: var(--font-code);
    font-size: 0.68rem;
  }

  .legend .sw {
    margin-right: 0.4em;
  }

  .legend .line {
    display: inline-block;
    width: 1.6em;
    margin-right: 0.4em;
    border-top: 2px dotted var(--ink);
    vertical-align: 0.25em;
  }

  .legend .corner {
    display: inline-block;
    width: 0.9em;
    height: 0.7em;
    margin-right: 0.4em;
    background: color-mix(in srgb, var(--ok) 14%, var(--sheet));
    border: 1px solid var(--stroke);
    vertical-align: -0.02em;
  }

  .hit {
    fill: transparent;
  }

  /* A 2px sheet ring keeps overlapping marks apart without an ink border. */
  .mk {
    fill: var(--c, var(--prov-other));
    stroke: var(--sheet);
    stroke-width: 2;
  }

  rect.mk {
    stroke: var(--ink);
    stroke-width: 1;
  }

  .lbl {
    fill: var(--ink);
    font-size: 10.5px;
    paint-order: stroke;
    stroke: var(--sheet);
    stroke-width: 3px;
  }
</style>
