import { describe, expect, it } from "vitest";

import { formatDate, groupByMonth } from "./blog.js";

describe("blog date helpers", () => {
  it("formats a post date in UTC", () => {
    expect(formatDate("2026-09-01")).toBe("September 1, 2026");
  });

  it("groups newest-first posts into ordered months", () => {
    const posts = [
      { slug: "new", title: "New", date: "2026-09-10" },
      { slug: "same-month", title: "Same", date: "2026-09-01" },
      { slug: "old", title: "Old", date: "2026-08-22" },
    ];

    expect(groupByMonth(posts)).toEqual([
      {
        key: "2026-09",
        label: "September 2026",
        posts: posts.slice(0, 2),
      },
      { key: "2026-08", label: "August 2026", posts: posts.slice(2) },
    ]);
  });
});
