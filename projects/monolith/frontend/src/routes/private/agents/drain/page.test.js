// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

const mounted = [];

function listJob(overrides = {}) {
  return {
    name: "qd-a",
    state: "ok",
    outcome: "report",
    prompt_head: "Audit the repository",
    summary_head: "Audited the repository",
    last_run_at: "2026-08-29T12:00:00Z",
    next_run_at: null,
    locked_at: null,
    session: null,
    ...overrides,
  };
}

function frame(job) {
  return {
    now: "2026-08-29T12:01:00Z",
    lane: { state: "idle", cycle: null, error: null },
    recent_cycles: [],
    queue: { running: 0, due: 0, scheduled: 0, error: 0, ok: 1, parked: 0 },
    jobs: [job],
  };
}

async function renderPage(job, detailJob = null) {
  const fetchMock = vi.fn(async (url) => ({
    ok: true,
    json: async () =>
      String(url).endsWith("/console")
        ? frame(job)
        : { job: detailJob, attempts: [], now: "2026-08-29T12:01:00Z" },
  }));
  vi.stubGlobal("fetch", fetchMock);

  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Page, { target });
  mounted.push({ component, target });
  await vi.waitFor(() =>
    expect(target.querySelector(".job-row")).not.toBeNull(),
  );

  if (detailJob) {
    target.querySelector(".job-row").click();
    await tick();
    await vi.waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/agents/drain/jobs/qd-a"),
    );
    await vi.waitFor(() =>
      expect(target.querySelector(".job-detail .detail-pre")).not.toBeNull(),
    );
  }
  return target;
}

afterEach(async () => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("drain outcomes", () => {
  test("renders report markdown instead of a summary pre block", async () => {
    const summary =
      "Results for #5405:\n\n- first item\n- used **bold** and `code`";
    const target = await renderPage(listJob(), {
      ...listJob(),
      prompt: "Audit the repository",
      last_summary: summary,
    });

    expect(target.querySelector(".result-md li")).not.toBeNull();
    expect(target.querySelector(".result-md code").textContent).toBe("code");
    expect(target.querySelector(".result-md").innerHTML).not.toContain("**");
    expect(target.querySelector(".result-md").innerHTML).toContain("<strong>");
    expect(target.querySelector(".result-md a").href).toBe(
      "https://github.com/jomcgi/homelab/issues/5405",
    );
    expect(target.querySelectorAll(".job-detail .detail-pre")).toHaveLength(1);
  });

  test("renders an enriched PR card with state and diff counts", async () => {
    const url = "https://github.com/jomcgi-org/homelab/pull/456";
    const target = await renderPage(
      listJob({
        outcome: "pr",
        summary_head: url,
        pr: { url, number: 456 },
      }),
      {
        ...listJob({ outcome: "pr" }),
        repo: "jomcgi-org/homelab",
        branch: "feat/drain-links",
        prompt: "Audit the repository",
        last_summary: `Opened #5405. ${url}`,
        pr: {
          url,
          number: 456,
          repo: "jomcgi-org/homelab",
          title: "Classify drain outcomes",
          state: "closed",
          merged: true,
          changed_files: 3,
          additions: 15,
          deletions: 8,
        },
      },
    );

    const card = target.querySelector(".pr-card");
    expect(card).not.toBeNull();
    expect(card.textContent).toContain("#456");
    expect(card.querySelector(".pr-state-merged").textContent.trim()).toBe(
      "merged",
    );
    expect(card.textContent).toContain("Classify drain outcomes");
    expect(card.textContent.replace(/\s+/g, " ")).toContain("3 files +15 −8");
    expect(target.querySelector(".detail-repo").href).toBe(
      "https://github.com/jomcgi-org/homelab/tree/feat/drain-links",
    );
    expect(target.querySelector(".result-md a").href).toBe(
      "https://github.com/jomcgi-org/homelab/issues/5405",
    );
    expect(target.querySelectorAll(".job-detail .detail-pre")).toHaveLength(1);
  });

  test("renders a link when PR enrichment is unavailable", async () => {
    const url = "https://github.com/jomcgi/homelab/pull/123";
    const target = await renderPage(
      listJob({
        outcome: "pr",
        summary_head: url,
        pr: { url, number: 123 },
      }),
      {
        ...listJob({ outcome: "pr" }),
        prompt: "Audit the repository",
        last_summary: url,
        pr: { url, number: 123, repo: "jomcgi/homelab" },
      },
    );

    const link = target.querySelector(".pr-card a");
    expect(link.textContent.trim()).toBe("#123");
    expect(link.href).toBe(url);
    expect(target.querySelector(".pr-title")).toBeNull();
    expect(target.querySelector(".pr-stats")).toBeNull();
  });
});

describe("job row outcome text", () => {
  test("successful rows prefer the summary", async () => {
    const target = await renderPage(listJob());
    const row = target.querySelector(".job-row");

    expect(row.textContent).toContain("Audited the repository");
    expect(row.textContent).not.toContain("Audit the repository");
  });

  test("error rows show the summary and never the prompt", async () => {
    const target = await renderPage(
      listJob({
        state: "error",
        outcome: "error",
        summary_head: "turn timed out",
      }),
    );
    const row = target.querySelector(".job-row");

    expect(row.textContent).toContain("turn timed out");
    expect(row.textContent).not.toContain("Audit the repository");
    expect(row.querySelector(".err-text")).not.toBeNull();
  });

  test("PR rows expose separate toggle and link controls", async () => {
    const url = "https://github.com/jomcgi/homelab/pull/5405";
    const target = await renderPage(
      listJob({
        outcome: "pr",
        pr: { url, number: 5405 },
      }),
    );
    const row = target.querySelector(".job-row");
    const toggle = row.querySelector(".job-toggle");
    const link = row.querySelector(".pr-ref");

    expect(row.tagName).toBe("DIV");
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(link.tagName).toBe("A");
    expect(link.href).toBe(url);
    expect(link.target).toBe("_blank");
    expect(link.rel).toBe("noreferrer");

    link.click();
    await tick();
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});
