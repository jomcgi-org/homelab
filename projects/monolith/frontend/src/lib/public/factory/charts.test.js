import { describe, expect, it } from "vitest";
import {
  barChartSvg,
  chartScale,
  last14,
  sparkPaths,
  window,
} from "./charts.js";

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

  it("labels both ends of a month without printing the last date twice", () => {
    // The bug this guards: a tick was drawn for every seventh day and again
    // for the last day, and over a thirty day window index 28 and index 29 are
    // one bar apart, so every chart printed its final date twice.
    const start = Date.UTC(2026, 7, 10);
    const days = Array.from({ length: 30 }, (_, index) => ({
      d: new Date(start + index * 86400000).toISOString().slice(0, 10),
      value: index,
    }));
    const svg = barChartSvg("month", days, ["value"], ["var(--tone-gpu)"]);
    const labels = [...svg.matchAll(/>(\d\d·\d\d)<\/text>/g)].map(
      (match) => match[1],
    );

    expect(new Set(labels).size).toBe(labels.length);
    expect(labels.at(0)).toBe("08·10");
    expect(labels.at(-1)).toBe("09·08");
    expect(labels).not.toContain("09·07");
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

  it("stacks Ember and local lane values in one bar", () => {
    const svg = barChartSvg(
      "sessions",
      [{ d: "2026-09-07", luna: 2, claude: 3 }],
      ["luna", "claude"],
      ["var(--tone-gpu)", "var(--tone-hot)"],
    );

    expect(svg.match(/<rect /g)).toHaveLength(2);
    expect(svg).toContain('fill="var(--tone-gpu)"');
    expect(svg).toContain('fill="var(--tone-hot)"');
  });
});

describe("UTC windows", () => {
  it("aligns the trailing 14 days to injected today", () => {
    expect(
      last14(
        [{ day: "2026-09-06", sessions: 3 }],
        "sessions",
        new Date("2026-09-07T23:59:59Z"),
      ),
    ).toEqual([0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 0]);
  });

  it("anchors a dense 30-day window to injected today, not newest data", () => {
    const rows = window(
      [
        { d: "2026-08-09", verified: 2 },
        { d: "2026-09-01", verified: 1 },
        { d: "2026-09-08", verified: 99 },
      ],
      "2026-09-07",
    );

    expect(rows).toHaveLength(30);
    expect(rows[0]).toMatchObject({ d: "2026-08-09", verified: 2 });
    expect(rows.at(-1)).toEqual({ d: "2026-09-07" });
  });
});

describe("phone chart layout", () => {
  it("uses a legible aspect ratio and labels both range ends inside the viewBox", () => {
    const svg = barChartSvg(
      "phone",
      [
        { d: "2026-09-01", luna: 4 },
        { d: "2026-09-30", luna: 6 },
      ],
      ["luna"],
      ["var(--tone-gpu)"],
      "Sessions per day",
    );
    expect(svg).toContain('viewBox="0 0 360 192"');
    expect(svg).toContain(
      'font-size="15" text-anchor="start" fill="currentColor">09·01',
    );
    expect(svg).toContain(
      'font-size="15" text-anchor="end" fill="currentColor">09·30',
    );
    expect(svg).toContain('aria-label="Sessions per day"');
    expect(svg).toContain("<desc>2026-09-01 to 2026-09-30. luna: 10.</desc>");
  });

  it("escapes chart labels and descriptions without changing the underlying totals", () => {
    const svg = barChartSvg(
      "phone",
      [{ d: "2026-09-01", "<bad>": 3 }],
      ["<bad>"],
      ["var(--tone-gpu)"],
      'Sessions <script> & "test"',
    );
    expect(svg).not.toContain("<script>");
    expect(svg).not.toContain("<bad>");
    expect(svg).toContain(
      'aria-label="Sessions &lt;script&gt; &amp; &quot;test&quot;"',
    );
    expect(svg).toContain("&lt;bad&gt;: 3.");
  });

  it("names the empty state without inventing a dated series", () => {
    const svg = barChartSvg("empty", [], ["luna"], ["var(--tone-gpu)"]);
    expect(svg).toContain("<desc>No recorded days.</desc>");
    expect(svg).not.toContain("<rect ");
  });
});

describe("date label spacing", () => {
  it.each([7, 8, 14, 18, 25, 26, 27, 30, 60, 90])(
    "keeps boundary and weekly labels apart in a %i-day range",
    (length) => {
      const start = Date.UTC(2026, 8, 1);
      const rows = Array.from({ length }, (_, index) => ({
        d: new Date(start + index * 86400000).toISOString().slice(0, 10),
        value: index + 1,
      }));
      const svg = barChartSvg("spacing", rows, ["value"], ["var(--tone-gpu)"]);
      const labels = [
        ...svg.matchAll(
          /<text x="([\d.]+)" y="185" font-size="15" text-anchor="(start|middle|end)" fill="currentColor">(\d\d·\d\d)<\/text>/g,
        ),
      ];
      expect(labels[0][3]).toBe("09·01");
      expect(labels.at(-1)[3]).toBe(rows.at(-1).d.slice(5).replace("-", "·"));
      let previousEnd = 0;
      for (const [, x, anchor] of labels) {
        const width = 97.5;
        const start =
          Number(x) -
          (anchor === "end" ? width : anchor === "middle" ? width / 2 : 0);
        expect(start).toBeGreaterThanOrEqual(previousEnd);
        previousEnd = start + width;
        expect(previousEnd).toBeLessThanOrEqual(360);
      }
    },
  );
});

it("describes series using the same language as their visible legend", () => {
  const svg = barChartSvg(
    "facts",
    [{ d: "2026-09-01", v: 3, u: 2 }],
    ["v", "u"],
    ["var(--tone-ram)", "hatch"],
    "Facts written per day",
    ["verified", "unverified"],
  );
  expect(svg).toContain("verified: 3; unverified: 2.");
});

it("leaves room for enlarged numeric ticks without removing the scale", () => {
  const svg = barChartSvg(
    "large",
    [{ d: "2026-09-01", n: 99999 }],
    ["n"],
    ["var(--tone-gpu)"],
  );
  expect(svg).toContain(">100K</text>");
  expect(svg).toContain(">40K</text>");
  expect(svg.match(/text-anchor="end" fill="currentColor">/g)).toHaveLength(3);
  expect(svg).toContain('x="78" y="29.5" font-size="15"');
});

it("keeps the colored plot wide when the scale only needs two digits", () => {
  const svg = barChartSvg(
    "small",
    [{ d: "2026-09-01", n: 37 }],
    ["n"],
    ["var(--tone-gpu)"],
  );
  expect(svg).toContain('data-axis-digits="2"');
  expect(svg).toContain('<line x1="44"');
});
