import { describe, expect, it } from "vitest";
import {
  CALLS,
  CHAT_AT,
  CHAT_SCHEDULE,
  PHASES,
  clamp,
  easeInOut,
  sub,
} from "./timeline.js";

describe("agentstory timeline", () => {
  const order = [
    "heroOut",
    "wake",
    "hydrate",
    "creds",
    "park",
    "resume",
    "out",
  ];
  it("has ordered windows within the master range", () => {
    let end = 0;
    for (const name of order) {
      const [a, b] = PHASES[name];
      expect(a).toBeGreaterThanOrEqual(end);
      expect(a).toBeGreaterThanOrEqual(0);
      expect(b).toBeLessThanOrEqual(1);
      expect(b).toBeGreaterThan(a);
      end = b;
    }
    expect(PHASES.heroOut[0]).toBe(0);
    expect(PHASES.out[1]).toBe(1);
  });
  it("provides pure clamp, sub, and symmetric easing", () => {
    expect(clamp(-1, 0, 1)).toBe(0);
    expect(clamp(2, 0, 1)).toBe(1);
    expect(sub(0.3, 0.2, 0.4)).toBeCloseTo(0.5);
    expect(easeInOut(0)).toBe(0);
    expect(easeInOut(1)).toBe(1);
    expect(easeInOut(0.25) + easeInOut(0.75)).toBeCloseTo(1);
  });
  it("keeps wire calls and chat thresholds valid", () => {
    for (const [beat, calls] of Object.entries(CALLS)) {
      expect(PHASES[beat]).toBeDefined();
      for (const { a, b } of calls) {
        expect(a).toBeGreaterThanOrEqual(0);
        expect(a).toBeLessThan(b);
        expect(b).toBeLessThanOrEqual(1);
      }
    }
    for (let i = 1; i < CHAT_AT.length; i++)
      expect(CHAT_AT[i]).toBeGreaterThan(CHAT_AT[i - 1]);
    for (const at of CHAT_AT) expect(at).toBeGreaterThan(0).toBeLessThan(1);
    for (const [at, beat] of CHAT_SCHEDULE) {
      expect(at).toBeGreaterThanOrEqual(PHASES[beat][0]);
      expect(at).toBeLessThanOrEqual(PHASES[beat][1]);
    }
  });
});
