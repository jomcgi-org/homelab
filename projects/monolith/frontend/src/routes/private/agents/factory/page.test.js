// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

const mounted = [];

function node(key, state, deps = [], model = "sonnet", attempts = []) {
  const [kind] = key.split("_");
  return {
    node_key: key,
    label: key.replace("_", " · ").replaceAll("_", " "),
    kind,
    model,
    deps,
    state,
    max_cost_usd: 4,
    created_in_version: 1,
    attempts,
    session: attempts.at(-1)?.session ?? null,
  };
}

function receipt(overrides = {}) {
  return {
    id: 11,
    issue_number: 5980,
    generation: 3,
    title: "probes park their guest",
    url: "https://github.com/jomcgi-org/homelab/issues/5980",
    state: "admitted",
    task_id: "t-1",
    task_paused: false,
    cancellation_requested: false,
    admitted_at: "2026-09-10T04:57:48Z",
    deadline_at: new Date(Date.now() + 3600_000).toISOString(),
    turns_used: 2,
    committed_cost_usd: 8,
    unresolved_starts: 1,
    limits: {},
    evidence: null,
    policy: {
      conductor_model: "spark",
      worker_model: "sonnet",
      reviewer_model: "opus",
      max_task_turns_hard: 9,
      task_budget_usd: 36,
      max_attempts: 2,
    },
    starts: [
      {
        start_key: "factory-node:t-1:conductor_1:1",
        model: "spark",
        status: "succeeded",
        cost_usd: null,
        session_id: 3540,
      },
    ],
    nodes: [
      node("conductor_1", "done", [], "spark", [
        {
          attempt: 1,
          status: "succeeded",
          cost_usd: null,
          session_id: 3540,
          created_at: "2026-09-10T04:58:19Z",
          finished_at: null,
          session: {
            id: 3540,
            model: "spark",
            status: "completed",
            guest_bound: true,
            last_turn_at: new Date(Date.now() - 120_000).toISOString(),
            turns: 1,
            cost_usd: null,
            result_head: "Chose add_node investigate",
          },
        },
      ]),
      node("investigate_probe_park", "done", ["conductor_1"]),
      node("implement_fix", "running", ["investigate_probe_park"]),
    ],
    stop_events: [],
    ...overrides,
  };
}

function board(overrides = {}) {
  return {
    ok: true,
    state: "enabled",
    version: 44,
    actor: "operator",
    admitted_count: 11,
    lanes: {
      delivery: { limit: 1, active: 1, queued: 1 },
      advisory: { limit: 2, active: 0, queued: 0 },
    },
    quota_guard: {
      paused: false,
      state: "open",
      pause_percent: 85,
      resume_percent: 75,
      used_percent: 41,
    },
    policy: {
      generation: 3,
      conductor_model: "spark",
      worker_model: "sonnet",
      reviewer_model: "opus",
      max_tasks: 1,
      max_task_turns_hard: 9,
      task_budget_usd: 36,
    },
    active: [receipt()],
    queued: [
      receipt({
        id: 12,
        issue_number: 5981,
        state: "queued",
        task_id: null,
        nodes: [],
        starts: [],
      }),
    ],
    recent: [
      receipt({
        id: 10,
        issue_number: 5971,
        state: "failed",
        task_id: "t-0",
        nodes: [],
        evidence: { reason: "muse could not reach api.meta.ai" },
      }),
    ],
    ...overrides,
  };
}

function renderPage(data) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(Page, { target, props: { data } });
  mounted.push({ component, target });
  return target;
}

afterEach(() => {
  while (mounted.length) {
    const { component, target } = mounted.pop();
    unmount(component);
    target.remove();
  }
  vi.unstubAllGlobals();
});

describe("factory board page", () => {
  test("renders the state strip, the three sections, and the plan", () => {
    const target = renderPage({ board: board(), task: null, error: false });

    const stats = target.querySelector(".stats");
    expect(stats.textContent).toContain("enabled");
    expect(stats.textContent).toContain("spark");
    const labels = [...target.querySelectorAll(".sec-label")].map((el) =>
      el.textContent.trim(),
    );
    expect(labels[0]).toMatch(/^\/ In flight/);
    expect(labels[1]).toMatch(/^\/ Queue/);
    expect(labels[2]).toMatch(/^\/ Recent/);
    expect(target.querySelectorAll(".panel")).toHaveLength(3);
    expect(target.querySelector(".policy-line").textContent).toContain(
      "1/1 delivery \u00b7 0/2 advisory",
    );

    const ranks = target.querySelectorAll(".panel.admitted .dag .rank");
    expect(ranks).toHaveLength(3);
    expect(target.querySelector(".node.running").textContent).toContain(
      "implement",
    );
    expect(target.querySelector(".panel.admitted .spec").textContent).toContain(
      "2 of 9",
    );
  });

  test("opening a task shows its nodes, sessions, and the conductor link", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, json: async () => board() })),
    );
    const target = renderPage({ board: board(), task: null, error: false });

    target.querySelector(".panel.admitted .panel-head").click();
    await tick();

    const detail = target.querySelector(".panel.admitted .detail");
    expect(detail).not.toBeNull();
    const session = detail.querySelector("a.session");
    expect(session.getAttribute("href")).toBe("/agents?session=3540");
    expect(session.textContent).toContain("Chose add_node investigate");
    const talk = detail.querySelector("a.talk");
    const href = new URL(talk.getAttribute("href"), "https://example.test");
    expect(href.pathname).toBe("/agents");
    expect(href.searchParams.get("model")).toBe("spark");
    expect(href.searchParams.get("prompt")).toContain("t-1");
  });

  test("a queued card cannot open and a failed card shows its evidence", async () => {
    const target = renderPage({ board: board(), task: "t-0", error: false });

    const queued = target.querySelector(".panel.queued .panel-head");
    expect(queued.disabled).toBe(true);
    expect(target.querySelector(".panel.queued .spec").textContent).toContain(
      "waiting for a slot",
    );
    const failed = target.querySelector(".panel.failed");
    expect(failed.querySelector(".detail")).not.toBeNull();
    expect(failed.textContent).toContain("muse could not reach api.meta.ai");
  });

  test("opening a finished card without a plan fetches it with ?task=", async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => board(),
    }));
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ board: board(), task: null, error: false });

    target.querySelector(".panel.failed .panel-head").click();
    await tick();
    await vi.waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith("/agents/factory?task=t-0"),
    );
  });

  test("counts planning rounds beside the work turns, not inside them", () => {
    const planning = receipt({ turns_used: 2, planner_turns_used: 5 });
    const target = renderPage({
      board: board({ active: [planning] }),
      task: null,
      error: false,
    });
    expect(target.querySelector(".panel.admitted .spec").textContent).toContain(
      "2 of 9",
    );
    expect(target.querySelector(".panel.admitted .spec").textContent).toContain(
      "5 planning",
    );
  });

  test("renders a zero spend as $0.00 rather than a blank", () => {
    const fresh = receipt({ committed_cost_usd: 0, turns_used: 0 });
    const target = renderPage({
      board: board({ active: [fresh] }),
      task: null,
      error: false,
    });
    expect(target.querySelector(".panel.admitted .spec").textContent).toContain(
      "$0.00 of $36.00",
    );
  });

  test("says so when the claude window is holding delivery back", () => {
    const held = board({
      quota_guard: {
        paused: true,
        state: "paused",
        pause_percent: 85,
        resume_percent: 75,
        used_percent: 91.4,
      },
    });
    const target = renderPage({ board: held, task: null, error: false });
    expect(target.querySelector(".policy-line").textContent).toContain(
      "delivery held: claude 7d at 91% of 85%",
    );
  });

  test("says nothing about an open quota guard", () => {
    const target = renderPage({ board: board(), task: null, error: false });
    expect(target.querySelector(".policy-line .guard")).toBeNull();
  });

  test("says so when the board is unavailable", () => {
    const target = renderPage({ board: null, task: null, error: true });
    expect(target.querySelector(".stats").textContent).toContain("unavailable");
    expect(target.querySelector(".policy-line").textContent).toContain(
      "board unavailable",
    );
  });
});
