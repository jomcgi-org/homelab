import { describe, expect, it } from "vitest";
import { barChartSvg, chartScale, lastDays, sparkPaths } from "./charts.js";

describe("chartScale", () => {
  it("chooses readable one, two, and five-family ticks", () => {
    expect(chartScale(0)).toEqual({ step: 1, top: 1 });
    expect(chartScale(9)).toEqual({ step: 2, top: 10 });
    expect(chartScale(37)).toEqual({ step: 10, top: 40 });
    expect(chartScale(999)).toEqual({ step: 200, top: 1000 });
  });
});

describe("sparkPaths", () => {
  it("scales the largest value to the top inset and ends at today", () => {
    const path = sparkPaths([0, 5, 10]);
    expect(path.line).toBe("M0.0 22.0L50.0 12.0L100.0 2.0");
    expect(path.endX).toBe("100.0");
    expect(path.endY).toBe("2.0");
  });

  it("handles an empty series", () => {
    expect(sparkPaths([]).line).toBe("M0.0 22.0");
  });
});

describe("barChartSvg", () => {
  it("fills missing calendar days and uses only reached tick labels", () => {
    const svg = barChartSvg(
      "test",
      [
        { d: "2026-09-01", value: 3 },
        { d: "2026-09-03", value: 9 },
      ],
      ["value"],
      ["var(--tone-gpu)"],
    );

    expect(svg).toContain(">10</text>");
    expect(svg).toContain("09·01");
    expect(svg).toContain("09·03");
    expect(svg.match(/<rect /g)).toHaveLength(2);
  });

  it("does not interpolate unsafe ids, dates, or colors", () => {
    const svg = barChartSvg(
      'x"><script>',
      [
        { d: "2026-09-01", value: 4 },
        { d: '<script>alert("x")</script>', value: 4 },
      ],
      ["value"],
      ['"><script>'],
    );

    expect(svg).not.toContain("<script>");
    expect(svg).toContain('fill="currentColor"');
  });
});

describe("lastDays", () => {
  it("aligns sparse data to a fixed trailing window", () => {
    expect(
      lastDays(
        [{ day: "2026-09-06", sessions: 3 }],
        "sessions",
        "2026-09-07",
        3,
      ),
    ).toEqual([0, 3, 0]);
  });
});
