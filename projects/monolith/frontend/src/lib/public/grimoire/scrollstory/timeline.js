// Pure scrub math for the public "From scan to query" scroll story. No DOM,
// no Svelte: ScrollStory.svelte feeds it a scroll fraction and a set of
// element refs, and applies the results imperatively (the ExploreCanvas
// discipline). Ported verbatim from the approved reference mockup at
// ./reference-mockup.html so the choreography survives the move to Svelte.
//
// Everything here is a pure function of its arguments, which is the whole
// point of the split: the scrub timing is the only genuinely unit-testable
// part, so it lives away from the 60fps DOM writes and gets tested directly.

export const clamp = (v, a, b) => Math.min(Math.max(v, a), b);

export const lerp = (a, b, t) => a + (b - a) * t;

// Map a master fraction t into a sub-window [a, b], clamped to 0..1. This is
// how each animation carves its own slice out of the shared timeline.
export const sub = (t, a, b) => clamp((t - a) / (b - a), 0, 1);

// Named easing personalities: different physical events want different
// curves (see the reference mockup's notes). Each maps 0->0 and 1->1.
export const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - (-2 * t + 2) ** 2 / 2); // smooth in/out, the default
export const outCubic = (t) => 1 - (1 - t) ** 3; // heavy object settling
export const outQuart = (t) => 1 - (1 - t) ** 4; // crisp scanner strokes
export const inOutCubic = (t) =>
  t < 0.5 ? 4 * t ** 3 : 1 - (-2 * t + 2) ** 3 / 2; // swooping flight
export const outBack = (t) => 1 + 2.2 * (t - 1) ** 3 + 1.2 * (t - 1) ** 2; // pop with overshoot
export const outExpo = (t) => (t >= 1 ? 1 : 1 - 2 ** (-10 * t)); // counters rushing in

// Each phase owns a contiguous slice of the master timeline. `hold` is where
// the choreography finishes (leaving a plateau to dwell on); `rest` is the
// snap anchor inside that plateau. Contiguous and covering [0, 1].
export const PHASES = [
  { id: "hero", start: 0.0, end: 0.08, rest: 0.0, label: "PAGE" },
  {
    id: "layout",
    start: 0.08,
    end: 0.26,
    hold: 0.2,
    rest: 0.22,
    label: "PAGE",
  },
  {
    id: "chunks",
    start: 0.26,
    // The chunk cards keep flying to their resting slots until rawFly ends at
    // t~=0.42 (see cardFp in ScrollStory.frame), so hold/rest sit at the tail
    // of that window. The old rest of 0.40 snapped ~0.02 early, catching the
    // reader before the last cards had landed ("scroll past the animation").
    end: 0.44,
    hold: 0.42,
    rest: 0.43,
    label: "PASSAGES",
  },
  {
    id: "entities",
    start: 0.44,
    end: 0.66,
    hold: 0.56,
    rest: 0.6,
    label: "WHO AND WHAT",
  },
  {
    id: "scale",
    start: 0.66,
    end: 0.82,
    hold: 0.76,
    rest: 0.79,
    label: "EVERYTHING",
  },
  { id: "chat", start: 0.82, end: 1.0, hold: 0.97, rest: 1.0, label: "ASK" },
];

// The phase containing t, clamped so t <= 0 lands in the first phase and
// t >= 1 (or any gap) falls through to the last.
export function phaseAt(t) {
  const c = clamp(t, 0, 1);
  return (
    PHASES.find((p) => c >= p.start && c < p.end) ?? PHASES[PHASES.length - 1]
  );
}

// A phase's own 0..1 progress at master fraction t, clamped at both ends.
export function progressIn(phase, t) {
  return clamp((t - phase.start) / (phase.end - phase.start), 0, 1);
}

// Split text into segments, marking every non-overlapping occurrence of every
// phrase. Phrases are matched longest-first so "Wave Echo Cave" wins over
// "Cave"; anything shorter than 4 chars is skipped (too noisy). Each phrase is
// { phrase, color }; a marked segment carries { t, c: color }, plain text just
// { t }. The segments always reassemble to the original text, so the caller
// can slice them (for the typed answer) without ever producing invalid markup.
export function segmentize(text, phrases) {
  const lower = text.toLowerCase();
  const ordered = [...phrases].sort(
    (a, b) => b.phrase.length - a.phrase.length,
  );
  const marks = [];
  for (const { phrase, color } of ordered) {
    const nm = phrase.toLowerCase();
    if (nm.length < 4) continue;
    let idx = 0;
    while ((idx = lower.indexOf(nm, idx)) !== -1) {
      const end = idx + nm.length;
      if (!marks.some((m) => idx < m.end && end > m.start)) {
        marks.push({ start: idx, end, color });
      }
      idx = end;
    }
  }
  marks.sort((a, b) => a.start - b.start);
  const segs = [];
  let pos = 0;
  for (const m of marks) {
    if (m.start > pos) segs.push({ t: text.slice(pos, m.start) });
    segs.push({ t: text.slice(m.start, m.end), c: m.color });
    pos = m.end;
  }
  if (pos < text.length) segs.push({ t: text.slice(pos) });
  return segs;
}

// Each chunk's source footprint on the page: the union of its boxes in
// fractional page coords, art excluded (a full-page illustration would make
// the footprint a giant slab). Returns null for chunks with no footprint.
// NB a bbox's chunkId is the chunk's marker REF PATH (/page/N/Kind/M), NOT its
// UUID; matching on id once made every footprint null and the chunking flight
// a silent no-op in prod. The test pins this.
export function cardFootprints(chunks, bboxes) {
  return chunks.map((c) => {
    const bs = bboxes.filter((b) => b.chunkId === c.ref && b.kind !== "art");
    if (!bs.length) return null;
    return {
      x: Math.min(...bs.map((b) => b.x)),
      y: Math.min(...bs.map((b) => b.y)),
      x2: Math.max(...bs.map((b) => b.x + b.w)),
      y2: Math.max(...bs.map((b) => b.y + b.h)),
    };
  });
}

// Deterministic seeded PRNG (mulberry32): identical output on server and
// client and across repeat visits, so SSR and hydration agree on the graph
// layout and nobody sees a reflow.
function mulberry32(a) {
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Deterministic force-directed layout for the mini graph, computed with no
// DOM so it can run at SSR (the no-JS static scene draws from these positions
// too). Returns the entities with normalized x/y in roughly [-1, 1]: a few
// hundred relaxation steps, then a label-collision pass so the pill labels do
// not overlap. Same constants and seed as the reference mockup.
export function graphLayout(entities, edges) {
  const rand = mulberry32(49);
  const nodes = entities.map((e) => ({
    ...e,
    x: rand() * 2 - 1,
    y: rand() * 2 - 1,
  }));
  const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const live = edges.filter((e) => byId[e.from] && byId[e.to]);
  for (let it = 0; it < 260; it++) {
    for (const a of nodes) {
      for (const b of nodes) {
        if (a === b) continue;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const d2 = dx * dx + dy * dy + 0.001;
        const f = 0.0028 / d2;
        a.x += dx * f;
        a.y += dy * f;
      }
    }
    for (const e of live) {
      const a = byId[e.from];
      const b = byId[e.to];
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.hypot(dx, dy) || 0.001;
      const f = (d - 0.5) * 0.02;
      a.x += (dx / d) * f;
      a.y += (dy / d) * f;
      b.x -= (dx / d) * f;
      b.y -= (dy / d) * f;
    }
    for (const n of nodes) {
      n.x *= 0.985;
      n.y *= 0.985;
    }
  }
  for (let it = 0; it < 60; it++) {
    for (const a of nodes) {
      for (const b of nodes) {
        if (a === b) continue;
        const aw = 0.09 + a.name.length * 0.008;
        const bw = 0.09 + b.name.length * 0.008;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        const ox = (aw + bw) / 2 - Math.abs(dx);
        const oy = 0.11 - Math.abs(dy);
        if (ox > 0 && oy > 0) {
          const push = 0.5 * Math.min(ox, 0.02) * Math.sign(dx || 0.01);
          a.x += push;
          b.x -= push;
          const pushY = 0.5 * Math.min(oy, 0.02) * Math.sign(dy || 0.01);
          a.y += pushY;
          b.y -= pushY;
        }
      }
    }
  }
  return nodes;
}
