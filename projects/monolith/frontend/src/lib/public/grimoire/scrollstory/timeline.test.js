import { describe, it, expect } from "vitest";
import {
  PHASES,
  phaseAt,
  progressIn,
  lerp,
  ease,
  outCubic,
  outQuart,
  inOutCubic,
  outBack,
  outExpo,
  segmentize,
  cardFootprints,
} from "./timeline.js";

describe("timeline phases", () => {
  it("cover [0,1] with contiguous, non-overlapping phases", () => {
    let end = 0;
    for (const p of PHASES) {
      expect(p.start).toBeCloseTo(end, 5);
      expect(p.end).toBeGreaterThan(p.start);
      end = p.end;
    }
    expect(end).toBeCloseTo(1, 5);
  });

  it("phaseAt returns the phase containing t, clamped at the ends", () => {
    expect(phaseAt(-0.5).id).toBe(PHASES[0].id);
    expect(phaseAt(0).id).toBe("hero");
    expect(phaseAt(0.5).id).toBe("entities");
    expect(phaseAt(0.999).id).toBe("chat");
    expect(phaseAt(1).id).toBe("chat");
    expect(phaseAt(2).id).toBe("chat");
  });

  it("progressIn maps a phase sub-range to 0..1, clamped", () => {
    const p = { start: 0.2, end: 0.4 };
    expect(progressIn(p, 0.1)).toBe(0);
    expect(progressIn(p, 0.2)).toBe(0);
    expect(progressIn(p, 0.3)).toBeCloseTo(0.5);
    expect(progressIn(p, 0.4)).toBe(1);
    expect(progressIn(p, 0.5)).toBe(1);
  });
});

describe("timeline easing", () => {
  const curves = { ease, outCubic, outQuart, inOutCubic, outBack, outExpo };

  it("map the boundaries 0->0 and 1->1", () => {
    for (const [name, fn] of Object.entries(curves)) {
      expect(fn(0), `${name}(0)`).toBeCloseTo(0, 10);
      expect(fn(1), `${name}(1)`).toBeCloseTo(1, 10);
    }
  });

  it("outBack overshoots in the middle but lands exactly on 1", () => {
    // The overshoot is what makes entities "pop" on arrival; it must still
    // resolve to exactly 1 or the final rest position drifts.
    expect(outBack(1)).toBe(1);
    expect(outBack(0.85)).toBeGreaterThan(1);
  });

  it("ease is symmetric around its midpoint", () => {
    expect(ease(0.5)).toBeCloseTo(0.5, 10);
  });

  it("lerp interpolates between its endpoints", () => {
    expect(lerp(10, 20, 0)).toBe(10);
    expect(lerp(10, 20, 1)).toBe(20);
    expect(lerp(10, 20, 0.5)).toBe(15);
  });
});

describe("segmentize", () => {
  const red = "var(--grim-type-creature)";
  const blue = "var(--grim-type-spell)";

  it("marks every occurrence of a phrase", () => {
    const segs = segmentize("Cave near the deeper Cave", [
      { phrase: "Cave", color: red },
    ]);
    const marked = segs.filter((s) => s.c === red);
    expect(marked).toHaveLength(2);
    expect(marked.every((s) => s.t === "Cave")).toBe(true);
  });

  it("prefers the longest phrase and never overlaps marks", () => {
    const segs = segmentize("Wave Echo Cave", [
      { phrase: "Cave", color: red },
      { phrase: "Wave Echo Cave", color: blue },
    ]);
    const marked = segs.filter((s) => s.c);
    expect(marked).toHaveLength(1);
    expect(marked[0].t).toBe("Wave Echo Cave");
    expect(marked[0].c).toBe(blue);
  });

  it("skips phrases shorter than 4 characters", () => {
    const segs = segmentize("an orc and an ok", [
      { phrase: "orc", color: red },
      { phrase: "ok", color: blue },
    ]);
    expect(segs.every((s) => !s.c)).toBe(true);
  });

  it("reassembles exactly to the original text", () => {
    const text =
      "Nezznar sent the Cragmaw goblins after Gundren and planted the Redbrands in Phandalin.";
    const segs = segmentize(text, [
      { phrase: "Cragmaw goblins", color: red },
      { phrase: "Redbrands", color: blue },
      { phrase: "Phandalin", color: red },
      { phrase: "Nezznar", color: blue },
    ]);
    expect(segs.map((s) => s.t).join("")).toBe(text);
  });

  it("returns the whole text as one plain segment when nothing matches", () => {
    const segs = segmentize("nothing to see here", [
      { phrase: "dragon", color: red },
    ]);
    expect(segs).toEqual([{ t: "nothing to see here" }]);
  });
});

describe("cardFootprints", () => {
  // Mirrors the baked data's shape: chunks carry BOTH a uuid id and a marker
  // ref path; bboxes point at their chunk via the REF PATH in chunkId. A
  // uuid-based lookup silently produced zero footprints in prod once; these
  // pins make that failure loud.
  const chunks = [
    { id: "uuid-a", ref: "/page/49/SectionHeader/4" },
    { id: "uuid-b", ref: "/page/49/Picture/0" },
  ];
  const bboxes = [
    {
      id: "b1",
      kind: "header",
      chunkId: "/page/49/SectionHeader/4",
      x: 0.1,
      y: 0.1,
      w: 0.2,
      h: 0.05,
    },
    {
      id: "b2",
      kind: "text",
      chunkId: "/page/49/SectionHeader/4",
      x: 0.1,
      y: 0.16,
      w: 0.3,
      h: 0.1,
    },
    {
      id: "b3",
      kind: "art",
      chunkId: "/page/49/Picture/0",
      x: 0.0,
      y: 0.0,
      w: 0.5,
      h: 0.6,
    },
    {
      id: "b4",
      kind: "caption",
      chunkId: "/page/49/Picture/0",
      x: 0.2,
      y: 0.61,
      w: 0.15,
      h: 0.02,
    },
  ];
  it("matches boxes to chunks by marker ref, unioning their rects", () => {
    const fp = cardFootprints(chunks, bboxes);
    expect(fp[0]).toEqual({ x: 0.1, y: 0.1, x2: 0.4, y2: 0.26 });
  });

  it("excludes art boxes but keeps their captions", () => {
    const fp = cardFootprints(chunks, bboxes);
    expect(fp[1]).toEqual({ x: 0.2, y: 0.61, x2: 0.35, y2: 0.63 });
  });

  it("yields NO footprints when matched by uuid (the prod no-op regression)", () => {
    const byUuid = chunks.map((c) => ({ ...c, ref: c.id }));
    expect(cardFootprints(byUuid, bboxes).every((f) => f === null)).toBe(true);
  });

  it("returns null for chunks with no boxes", () => {
    expect(
      cardFootprints([{ id: "x", ref: "/page/1/Text/9" }], bboxes),
    ).toEqual([null]);
  });
});
