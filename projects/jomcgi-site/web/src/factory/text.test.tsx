import { render } from "@testing-library/react";
import { expect, it } from "vitest";
import {
  axisLabel,
  formatInstant,
  median,
  sessionLabel,
  tallyCounts,
  tallyHours,
} from "./data";
import { Text } from "./Text";

it("renders the Markdown subset as elements and keeps HTML as text", () => {
  const { container } = render(
    <Text
      text={[
        "**Problem.** Run `ci lint` on *each* change.",
        "1. First",
        "   - nested <script>alert(1)</script>",
        "2. Second",
        "",
        "```",
        "**not bold**",
        "```",
      ].join("\n")}
    />,
  );
  expect(container.querySelector("strong")?.textContent).toBe("Problem.");
  expect(container.querySelector("code")?.textContent).toBe("ci lint");
  expect(container.querySelector("em")?.textContent).toBe("each");
  expect(container.querySelectorAll("ol > li")).toHaveLength(2);
  expect(container.querySelector("ol ul li")?.textContent).toBe(
    "nested <script>alert(1)</script>",
  );
  expect(container.querySelector("pre")?.textContent).toBe("**not bold**");
  expect(container.querySelector("script")).toBeNull();
});

it("links only this repository and keeps other link text", () => {
  const { container } = render(
    <Text text="[a](https://github.com/jomcgi-org/homelab/pull/1) [b](https://example.invalid/x) [c](javascript:alert(1))" />,
  );
  expect(
    [...container.querySelectorAll("a")].map((a) => a.getAttribute("href")),
  ).toEqual(["https://github.com/jomcgi-org/homelab/pull/1"]);
  expect(container.textContent).toBe("a b [c](javascript:alert(1))");
});

it("tallies durations and counts into fixed columns", () => {
  expect(tallyHours([0.5, 6, 23.9, 24, 500]).map(({ value }) => value)).toEqual(
    [1, 1, 1, 1, 0, 1],
  );
  expect(tallyCounts([1, 3, 3])).toEqual([
    { label: "1", value: 1 },
    { label: "2", value: 0 },
    { label: "3", value: 2 },
  ]);
  expect(tallyCounts([2, 12]).at(-1)).toEqual({ label: "8+", value: 1 });
  expect(tallyCounts([])).toEqual([]);
  expect(median([3, 1, 2])).toBe(2);
  expect(median([1, 2, 3, 4])).toBe(2.5);
  expect(median([])).toBeUndefined();
});

it("shortens dates, instants and generated session ids", () => {
  expect(axisLabel("Week of 2026-11-09")).toBe("9 Nov");
  expect(axisLabel("PR #8430")).toBe("PR #8430");
  expect(formatInstant("2026-11-20T04:31:59.348Z")).toBe(
    "20 Nov 2026, 04:31 UTC",
  );
  expect(formatInstant("soon")).toBe("soon");
  expect(sessionLabel(`session-${"ab12".repeat(16)}`)).toBe("ab12ab12");
  expect(sessionLabel("demo-build")).toBe("demo-build");
});
