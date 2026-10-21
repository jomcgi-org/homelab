import { describe, expect, it } from "vitest";
import {
  BORED_AFTER_S,
  INKS,
  RAMPS,
  RESTLESS_AFTER_S,
  applyLut,
  buildLut,
  driftAmplitude,
  glitchSchedule,
  hexToRgb,
  inkRamp,
  nextGap,
  stripSeed,
  STRIPS,
} from "./home-portrait.js";

describe("portrait tone ramps", () => {
  it("maps black to the ramp's dark end and white to the sheet, per scheme", () => {
    for (const scheme of ["light", "dark"]) {
      const lut = buildLut(RAMPS[scheme]);
      const [d, l] = [RAMPS[scheme][0], RAMPS[scheme][4]];
      expect([lut[0], lut[1], lut[2]]).toEqual(
        d.map((v) => Math.round(v * 255)),
      );
      expect([lut[255 * 3], lut[255 * 3 + 1], lut[255 * 3 + 2]]).toEqual(
        l.map((v) => Math.round(v * 255)),
      );
    }
  });

  it("interpolates between stops so a mid grey lands on the middle stop", () => {
    const lut = buildLut(RAMPS.light);
    // 127.5 is the exact middle; 128 is one step above it, so within a unit.
    const mid = RAMPS.light[2].map((v) => v * 255);
    expect(Math.abs(lut[128 * 3] - mid[0])).toBeLessThan(2);
  });

  it("recolours pixels in place from the red channel and leaves alpha alone", () => {
    const lut = buildLut(RAMPS.light);
    const px = new Uint8ClampedArray([0, 0, 0, 255, 255, 255, 255, 40]);
    applyLut(px, lut);
    expect([px[0], px[1], px[2], px[3]]).toEqual([lut[0], lut[1], lut[2], 255]);
    expect(px[7]).toBe(40);
  });

  it("re-inks the dark end only, keeping the scheme's light end", () => {
    const ramp = inkRamp("dark", "blue");
    expect(ramp[0]).toEqual(hexToRgb(INKS.blue[0]));
    expect(ramp[1]).toEqual(hexToRgb(INKS.blue[1]));
    expect(ramp.slice(2)).toEqual(RAMPS.dark.slice(2));
  });
});

describe("glitch clock", () => {
  it("quickens from calm to restless to bored to gone", () => {
    const calm = glitchSchedule({ idle: 0, gone: false, hidden: false });
    const restless = glitchSchedule({
      idle: RESTLESS_AFTER_S + 1,
      gone: false,
      hidden: false,
    });
    const bored = glitchSchedule({
      idle: BORED_AFTER_S + 1,
      gone: false,
      hidden: false,
    });
    const gone = glitchSchedule({ idle: 0, gone: true, hidden: false });
    const hidden = glitchSchedule({ idle: 0, gone: false, hidden: true });
    expect(calm.mean).toBeGreaterThan(restless.mean);
    expect(restless.mean).toBeGreaterThan(bored.mean);
    expect(gone.pool).toEqual(hidden.pool);
    expect(calm.pool).not.toContain("tint");
    expect(restless.pool).not.toContain("band");
    expect(bored.pool).toContain("tint");
    expect(bored.pool).toContain("band");
  });

  it("escalates to colour after thirty seconds of nothing", () => {
    expect(BORED_AFTER_S).toBe(30);
  });

  it("draws gaps from an exponential distribution above a floor", () => {
    expect(nextGap(1000, () => 0)).toBe(60);
    expect(nextGap(1000, () => 1 - Math.exp(-1))).toBeCloseTo(1060, 5);
  });
});

describe("strip seeds", () => {
  it("are stable and sit in [0, 1) for every strip, so scatter is symmetric", () => {
    const seeds = Array.from({ length: STRIPS }, (_, i) => stripSeed(i));
    for (const r of seeds) {
      expect(r).toBeGreaterThanOrEqual(0);
      expect(r).toBeLessThan(1);
    }
    expect(seeds.filter((r) => r < 0.5).length).toBeGreaterThan(STRIPS / 4);
    expect(seeds.filter((r) => r >= 0.5).length).toBeGreaterThan(STRIPS / 4);
    expect(stripSeed(7)).toBe(stripSeed(7));
  });
});

describe("drift", () => {
  it("is nothing for five seconds, then widens, with a higher ceiling once bored", () => {
    expect(driftAmplitude(0)).toBe(0);
    expect(driftAmplitude(5)).toBe(0);
    expect(driftAmplitude(10)).toBe(20);
    expect(driftAmplitude(BORED_AFTER_S)).toBe(30);
    expect(driftAmplitude(BORED_AFTER_S + 1)).toBeGreaterThan(30);
    expect(driftAmplitude(1000)).toBe(70);
  });
});
