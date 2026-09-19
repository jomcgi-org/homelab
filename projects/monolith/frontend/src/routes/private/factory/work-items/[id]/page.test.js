// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

const mounted = [];

function fixture() {
  return {
    item: {
      id: 100001,
      github_issue_number: 6257,
      source_ref: "https://github.com/owner/repo/issues/6257",
      state: "ready",
      authority: "github",
      trust: "trusted",
      task_class: "bug-fix",
      labels: ["agent-ready", "bug"],
      title: "Factory work item page",
    },
    edges_in: [
      { id: 1, from_id: 100002, kind: "blocks", source: "github_body" },
      { id: 2, from_id: 100003, kind: "parent", source: "decision" },
      { id: 3, from_id: 100004, kind: "supersedes", source: "manual" },
    ],
    edges_out: [
      { id: 4, to_id: 100005, kind: "blocks", source: "manual" },
      { id: 5, to_id: 100006, kind: "parent", source: "decision" },
      { id: 6, to_id: 100007, kind: "supersedes", source: "manual" },
    ],
    receipts: [
      {
        id: 41,
        generation: 2,
        task_class: "bug-fix",
        state: "escalated",
        created_at: new Date(Date.now() - 60_000).toISOString(),
        task_id: "task-41",
      },
      {
        id: 40,
        generation: 1,
        task_class: "refine",
        state: "succeeded",
        created_at: new Date(Date.now() - 120_000).toISOString(),
        task_id: "task-40",
      },
    ],
    events: [],
  };
}

function renderPage(data) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Page, { target, props: { data } });
  mounted.push({ component, target });
  return target;
}

async function settle() {
  for (let i = 0; i < 5; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await tick();
  }
}

function successfulFetch(document) {
  return vi.fn().mockResolvedValue({
    ok: true,
    json: async () => document,
  });
}

afterEach(() => {
  while (mounted.length) {
    const { component, target } = mounted.pop();
    unmount(component);
    target.remove();
  }
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("work item page", () => {
  test("renders identity, all edge directions, receipts, and the add control", () => {
    const target = renderPage({ itemId: "100001", document: fixture() });

    expect(target.querySelector(".item-id").textContent).toContain("100001");
    expect(target.querySelector(".issue-link").getAttribute("href")).toBe(
      "https://github.com/owner/repo/issues/6257",
    );
    expect(target.querySelector(".issue-link").textContent).toContain("#6257");
    expect(target.querySelector(".item-meta").textContent).toContain("ready");
    expect(target.querySelector(".item-meta").textContent).toContain("github");
    expect(target.querySelector(".item-meta").textContent).toContain("trusted");
    expect(target.querySelector(".item-meta").textContent).toContain("bug-fix");
    expect(target.querySelector(".item-meta").textContent).toContain(
      "agent-ready",
    );

    const sectionLabels = [...target.querySelectorAll(".sec-label")].map(
      (node) => node.textContent.trim(),
    );
    expect(sectionLabels).toEqual(
      expect.arrayContaining([
        "/ Blocked by",
        "/ Blocks",
        "/ Parent",
        "/ Children",
        "/ Supersedes",
        "/ Superseded by",
        "/ Receipts",
      ]),
    );
    expect(target.querySelectorAll(".edge-line")).toHaveLength(6);
    expect(target.querySelector("select[name=direction]")).not.toBeNull();
    expect(
      target.querySelectorAll("select[name=direction] option"),
    ).toHaveLength(5);
    expect(target.querySelector("[aria-label=Receipts]").textContent).toContain(
      "receipt 41",
    );
    expect(
      target.querySelector("[aria-label=Receipts] a").getAttribute("href"),
    ).toBe("/factory/escalations");
  });

  test("removes an edge through the proxy route", async () => {
    const document = fixture();
    const fetchMock = successfulFetch(document);
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ itemId: "100001", document });

    target.querySelector(".remove-btn").click();
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/factory/work-items/100001/edges/1",
    );
    expect(fetchMock.mock.calls[0][1]).toEqual({ method: "DELETE" });
  });

  test.each([
    ["#123", "#123"],
    ["456", 456],
  ])("submits %s in the backend representation", async (input, expected) => {
    const document = fixture();
    const fetchMock = successfulFetch(document);
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ itemId: "100001", document });
    const form = target.querySelector("form.add-form");
    form.direction.value = "parent|in";
    form.other.value = input;

    form.dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe("/factory/work-items/100001/edges");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      kind: "parent",
      direction: "in",
      other: expected,
      stated_reason: null,
    });
    expect(form.other.value).toBe("");
  });

  test("rejects an invalid other value without fetching", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ itemId: "100001", document: fixture() });
    const form = target.querySelector("form.add-form");
    form.other.value = "abc";

    form.dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    await tick();

    expect(fetchMock).not.toHaveBeenCalled();
    expect(target.querySelector("[role=alert]").textContent).toContain(
      "Enter work item id or #issue",
    );
  });

  test("renders a conflict response", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: "would create blocks cycle" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ itemId: "100001", document: fixture() });
    const form = target.querySelector("form.add-form");
    form.other.value = "456";

    form.dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
    await settle();

    expect(target.querySelector("[role=alert]").textContent).toContain(
      "would create blocks cycle",
    );
  });

  test("renders the requested id when the item is missing", () => {
    const target = renderPage({ itemId: "not-an-id", missing: true });
    expect(target.textContent).toContain("No work item not-an-id");
  });

  test("retries an error with the loader item id", async () => {
    vi.useFakeTimers();
    const fetchMock = successfulFetch(fixture());
    vi.stubGlobal("fetch", fetchMock);
    renderPage({ itemId: "100099", error: true });

    await vi.advanceTimersByTimeAsync(20000);
    await tick();

    expect(fetchMock.mock.calls[0][0]).toBe("/factory/work-items/100099");
  });
});
