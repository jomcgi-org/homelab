import { readFileSync } from "node:fs";
import { render, screen, waitFor } from "@testing-library/react";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { FactoryPage } from "../factory-page";
import { SECTIONS } from "../sections";
import { Pages } from "./Pages";
import { fixtureSnapshot, mockFiles } from "./test-fixtures";

const pages = [
  ["/factory", "A reading list that keeps your place"],
  ["/factory/goals/reading-list", "Return to the reading list"],
  ["/factory/issues/DEMO-104", "Open a saved reading list"],
  ["/factory/sessions/demo-build", "Remember the saved reading list"],
];

describe.each(["light", "dark"] as const)("%s theme", (mode) => {
  it.each(pages)(
    "%s has no axe violations or inline styles",
    async (path, title) => {
      window.history.replaceState(null, "", path);
      mockFiles();
      if (path === "/factory") {
        await import("../DashboardPlot");
        // Happy DOM has no layout. Supply a width so axe sees the rendered plots.
        vi.stubGlobal(
          "ResizeObserver",
          class {
            constructor(private readonly callback: ResizeObserverCallback) {}

            observe(target: Element) {
              this.callback(
                [
                  {
                    target,
                    contentRect: new DOMRect(0, 0, 360, 200),
                  } as ResizeObserverEntry,
                ],
                this as unknown as ResizeObserver,
              );
            }

            unobserve() {}

            disconnect() {}
          },
        );
      }
      const styles = [...document.querySelectorAll("style")];
      const { container } = render(
        <Theme theme={neutralTheme} mode={mode}>
          <FactoryPage />
        </Theme>,
      );
      await screen.findByRole("heading", { level: 1, name: title });
      if (path === "/factory") {
        await waitFor(() =>
          expect(
            container.querySelectorAll(".dashboard-chart-plot svg"),
          ).toHaveLength(4),
        );
        const singleBar = screen
          .getByRole("img", { name: /^Issue-to-merge time\./ })
          .querySelector("svg path[fill]");
        // The rounded path starts at the left edge. Its second curve
        // reaches the right edge of the same bar.
        const edges = singleBar
          ?.getAttribute("d")
          ?.match(/^M([\d.]+),[^H]+H[^Q]+Q([\d.]+),/);
        expect(edges).not.toBeNull();
        expect(Number(edges?.[2]) - Number(edges?.[1])).toBeCloseTo(32);
      }
      // Include every chart table and tool call in the accessibility check.
      for (const details of container.querySelectorAll("details"))
        details.open = true;
      const results = await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(
        results.violations.map(({ id, nodes }) => ({
          id,
          targets: nodes.map((node) => node.target),
        })),
      ).toEqual([]);
      expect(container.querySelectorAll("[style], script, style")).toHaveLength(
        0,
      );
      expect([...document.querySelectorAll("style")]).toEqual(styles);
    },
  );
});

it("keeps CSP, the client-route rewrite and the deferred Factory link", () => {
  const headers = readFileSync("public/_headers", "utf8");
  expect(headers).toContain("style-src 'self'");
  expect(headers).not.toMatch(/unsafe-inline|unsafe-eval/);
  expect(
    readFileSync("public/_redirects", "utf8").match(/^\/factory.*$/gm),
  ).toEqual(["/factory /factory/ 200", "/factory/* /factory/ 200"]);
  expect(SECTIONS.find(({ path }) => path === "/factory")).toMatchObject({
    live: false,
    legacy: "/slop/factory",
  });
});

it("keeps usable goal links when the story is not available", async () => {
  const snapshot = await fixtureSnapshot();
  render(<Pages path="/factory" snapshot={{ ...snapshot, story: null }} />);
  expect(screen.getByText("Story not available.")).toBeDefined();
  expect(
    screen
      .getByRole("link", { name: "Return to the reading list" })
      .getAttribute("href"),
  ).toBe("/factory/goals/reading-list");
});

it("never interprets tool input, output or diffs as HTML or Markdown", async () => {
  const snapshot = await fixtureSnapshot();
  const session = snapshot.sessions["demo-correction"]!;
  const payload =
    '<script>alert("fixture")</script> [link](https://example.invalid)';
  session.turns = [
    {
      number: 1,
      at: "2030-05-20T13:00:00Z",
      role: "assistant",
      withheld: false,
      text: payload,
      toolCalls: [{ name: payload, input: payload, output: payload }],
      diff: payload,
    },
  ];
  const { container } = render(
    <Pages path="/factory/sessions/demo-correction" snapshot={snapshot} />,
  );
  for (const pre of container.querySelectorAll("pre"))
    expect(pre.textContent?.trim()).toBe(payload);
  expect(
    container.querySelectorAll("script, a[href='https://example.invalid']"),
  ).toHaveLength(0);
});
