import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HomelabPage } from "./homelab-page";
import {
  REPO,
  REVIEWED,
  STALE_AFTER_DAYS,
  STATUS_LABEL,
  allProjects,
  containmentIntro,
  groups,
  intro,
  isStale,
  layout,
  limits,
  method,
  paths,
  positions,
  principles,
  reviewAge,
} from "./homelab-data";
import { Root } from "./root";

const mount = () =>
  render(
    <Root>
      <HomelabPage />
    </Root>,
  );

/** Every sentence the page prints, from its data. */
const copy = [
  intro,
  containmentIntro,
  method.body,
  ...layout.flatMap((part) => [part.title, part.body]),
  ...paths.flatMap((path) => [path.title, path.summary, ...path.steps]),
  ...principles.flatMap((principle) => [principle.title, principle.body]),
  ...positions.flatMap((position) => [position.who, position.reach]),
  ...limits,
  ...allProjects.flatMap((project) => [project.name, project.what]),
];

describe("homelab page", () => {
  afterEach(() => vi.useRealTimers());

  it("has one h1 and the four sections the index links to", () => {
    const { container } = mount();
    expect(container.querySelectorAll("h1")).toHaveLength(1);
    const headings = [...container.querySelectorAll("main h2")].map(
      (h) => h.textContent,
    );
    expect(headings).toEqual([
      "How it fits",
      "Containing a threat",
      "Method",
      "Projects",
    ]);
    for (const link of container.querySelectorAll(".homelab-index a")) {
      const id = link.getAttribute("href")!.slice(1);
      expect(container.querySelector(`#${id}`), id).toBeTruthy();
    }
  });

  it("lists every project once, with a known status and a repository link", () => {
    const { container } = mount();
    const names = allProjects.map((project) => project.name);
    expect(new Set(names).size).toBe(names.length);
    for (const group of groups)
      expect(group.projects.length).toBeGreaterThan(0);
    for (const project of allProjects) {
      expect(Object.keys(STATUS_LABEL)).toContain(project.status);
      expect(project.path).toMatch(/^projects\/[a-z0-9-]+$/);
      const link = screen.getByRole("link", { name: project.name });
      expect(link.getAttribute("href")).toBe(
        `${REPO}/tree/${project.ref ?? "main"}/${project.path}`,
      );
    }
    expect(container.querySelectorAll(".homelab-status")).toHaveLength(
      names.length,
    );
  });

  it("draws each path as an ordered list, with the arrows hidden from readers", () => {
    const { container } = mount();
    const lists = container.querySelectorAll("ol.homelab-steps");
    expect(lists).toHaveLength(paths.length);
    lists.forEach((list, index) => {
      const steps = paths[index]!.steps;
      expect(list.querySelectorAll("li")).toHaveLength(steps.length);
      expect(list.querySelectorAll(".homelab-arrow")).toHaveLength(
        steps.length - 1,
      );
      for (const arrow of list.querySelectorAll(".homelab-arrow"))
        expect(arrow.getAttribute("aria-hidden")).toBe("true");
    });
  });

  it("states what the design does not cover", () => {
    mount();
    expect(screen.getByRole("heading", { name: "What this does not cover" }));
    for (const limit of limits) expect(screen.getByText(limit)).toBeTruthy();
  });

  it("claims no freshness in the built HTML, and warns after 90 days", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(Date.parse(`${REVIEWED}T12:00:00Z`));
    const { container, unmount } = mount();
    await act(async () => {});
    expect(container.querySelector(".homelab-stale")).toBeNull();
    unmount();

    vi.setSystemTime(
      Date.parse(`${REVIEWED}T12:00:00Z`) +
        (STALE_AFTER_DAYS + 30) * 86_400_000,
    );
    const later = mount();
    await act(async () => {});
    expect(
      later.container.querySelector(".homelab-stale")?.textContent,
    ).toMatch(/last reviewed 120 days ago/);
  });
});

describe("review age", () => {
  const day = 86_400_000;
  it("counts whole days and never goes negative", () => {
    const at = Date.parse("2026-11-24T00:00:00Z");
    expect(reviewAge("2026-11-24", at + 3.9 * day)).toBe(3);
    expect(reviewAge("2026-11-24", at - 5 * day)).toBe(0);
  });
  it("is stale only after the limit", () => {
    const at = Date.parse("2026-11-24T00:00:00Z");
    expect(isStale("2026-11-24", at + STALE_AFTER_DAYS * day)).toBe(false);
    expect(isStale("2026-11-24", at + (STALE_AFTER_DAYS + 1) * day)).toBe(true);
  });
});

describe("homelab copy", () => {
  // The markers public_content.py rejects in the repository's public docs.
  const internal: [string, RegExp][] = [
    ["in-cluster hostname", /\.svc\.cluster\.local/],
    ["op:// reference", /op:\/\//],
    ["vault item path", /vaults\/[\w-]+\/items\//],
    [
      "private address",
      /\b(?:10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])|192\.168)(?:\.\d{1,3}){2}\b/,
    ],
    ["node name", /\bnode-\d+\b/],
    ["brick name", /\bbrick-\d+\b/],
    ["S3 URI", /\bs3:\/\//],
    [".internal hostname", /\b[\w-]+(?:\.[\w-]+)*\.internal\b(?!\.\w)/],
  ];

  it.each(internal)("holds no %s", (_label, pattern) => {
    for (const text of copy) expect(text).not.toMatch(pattern);
  });

  it("holds no tracker ids, em-dashes or banned phrases", () => {
    for (const text of copy) {
      expect(text).not.toMatch(/\bHOME-\d+\b/);
      expect(text).not.toContain("—");
      expect(text.toLowerCase()).not.toContain("load-bearing");
    }
  });
});
