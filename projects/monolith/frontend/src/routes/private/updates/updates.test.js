import { describe, expect, it } from "vitest";

import UpdatesPage from "./+page.svelte";
import {
  facetHref,
  formatDate,
  formatVersion,
  groupUpdatesByMonth,
  label,
} from "./updates.js";

describe("updates archive helpers", () => {
  it("compiles the archive page", () => {
    expect(UpdatesPage).toBeTypeOf("function");
  });

  it("groups newest-first entries into stable month sections", () => {
    const groups = groupUpdatesByMonth([
      { published_on: "2026-08-29", headline: "One" },
      { published_on: "2026-08-28", headline: "Two" },
      { published_on: "2026-07-31", headline: "Three" },
    ]);

    expect(groups.map((group) => group.key)).toEqual(["2026-08", "2026-07"]);
    expect(groups[0].label).toBe("August 2026");
    expect(groups[0].updates).toHaveLength(2);
  });

  it("formats dates, versions, and facet names", () => {
    expect(formatDate("2026-08-29")).toBe("August 29, 2026");
    expect(formatVersion("2026-08-29")).toBe("2026.08.29");
    expect(label("developer-tools")).toBe("Developer Tools");
  });

  it("combines filters and toggles an active filter off", () => {
    expect(facetHref("project", "monolith", "", "security")).toBe(
      "/updates?project=monolith&technology=security",
    );
    expect(facetHref("technology", "security", "monolith", "security")).toBe(
      "/updates?project=monolith",
    );
    expect(facetHref("project", "monolith", "monolith", "")).toBe("/updates");
  });
});
