import { describe, expect, it } from "vitest";
import { render } from "svelte/server";

import UpdatesPage from "./+page.svelte";
import {
  facetHref,
  formatDate,
  label,
  monthHref,
  monthLabel,
} from "./updates.js";

async function renderPage(overrides = {}) {
  const data = {
    updates: [],
    months: [],
    projects: [],
    technologies: [],
    selectedMonth: "",
    selectedProject: "",
    selectedTechnology: "",
    invalidMonth: false,
    error: false,
    ...overrides,
  };
  const { html } = await render(UpdatesPage, { props: { data } });
  return html.replace(/\s+/g, " ");
}

describe("updates archive helpers", () => {
  it("compiles the archive page", () => {
    expect(UpdatesPage).toBeTypeOf("function");
  });

  it("formats dates and facet names", () => {
    expect(formatDate("2026-08-29")).toBe("August 29, 2026");
    expect(monthLabel("2025-12")).toBe("December 2025");
    expect(label("developer-tools")).toBe("Developer Tools");
  });

  it("combines filters, preserves the month, and toggles a filter off", () => {
    expect(facetHref("project", "monolith", "", "security", "2026-08")).toBe(
      "/updates?project=monolith&technology=security&month=2026-08",
    );
    expect(
      facetHref("technology", "security", "monolith", "security", "2026-08"),
    ).toBe("/updates?project=monolith&month=2026-08");
    expect(facetHref("project", "monolith", "monolith", "")).toBe("/updates");
  });

  it("builds SSR month and edition links with facet filters intact", () => {
    expect(monthHref("2025-12", "monolith", "frontend")).toBe(
      "/updates?month=2025-12&project=monolith&technology=frontend",
    );
    expect(monthHref("2026-01", "monolith", "", "2026-01-02")).toBe(
      "/updates?month=2026-01&project=monolith#update-2026-01-02",
    );
  });

  it("renders distinct empty, selected-month, filtered, and error states", async () => {
    expect(await renderPage()).toContain(
      "Waiting for the first daily edition.",
    );
    const selectedMonthEmpty = await renderPage({
      months: [
        {
          month: "2025-12",
          count: 1,
          editions: [
            { published_on: "2025-12-05", headline: "December agents" },
          ],
        },
      ],
      selectedMonth: "2026-01",
    });
    expect(selectedMonthEmpty).toContain(
      "No updates were published in this month.",
    );
    expect(selectedMonthEmpty).toContain('href="/updates?month=2025-12"');
    expect(selectedMonthEmpty).toContain('aria-label="Open December 2025"');
    expect(selectedMonthEmpty).not.toMatch(
      /<summary\b[^>]*>(?:(?!<\/summary>)[\s\S])*<a\b/,
    );
    expect(
      await renderPage({
        months: [
          {
            month: "2025-12",
            count: 1,
            editions: [
              { published_on: "2025-12-05", headline: "December agents" },
            ],
          },
        ],
        selectedMonth: "2026-01",
        selectedProject: "monolith",
      }),
    ).toContain("No updates match these filters in this month.");
    expect(await renderPage({ error: true })).toContain(
      "The journal is unavailable.",
    );
    const invalidMonth = await renderPage({
      selectedMonth: "2026-13",
      invalidMonth: true,
    });
    expect(invalidMonth).toContain("That journal month is not valid.");
    expect(invalidMonth).toContain('href="/updates"');
    expect(invalidMonth).not.toContain("The journal is unavailable.");
  });
});
