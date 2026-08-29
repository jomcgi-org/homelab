// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import { createClassComponent } from "svelte/legacy";
import RunView from "./RunView.svelte";
import { clockTime } from "./run-history.js";

const mounted = [];

function gatedRun() {
  return {
    workflow_id: "wf/gated",
    dbos_status: "PENDING",
    state: "blocked",
    task: { text: "Approve a push" },
    created_at: "2026-08-22T11:00:00Z",
    updated_at: "2026-08-22T11:30:00Z",
    completed_at: null,
    cost_usd: 0,
    plan: { pinned: true, max_attempts: 2, turn_timeout_seconds: 1800 },
    disposition: {
      state: "gated",
      reason: "the branch is ready",
      next: "approve, send_back, retry",
    },
    nodes: [
      {
        key: "push_gate",
        kind: "gate",
        label: "push gate",
        state: "blocked",
        model: null,
        deps: [],
        attempts: [],
        blocked_on: {
          kind: "human",
          note: "Approve this branch for push?",
          since: "2026-08-22T11:15:00Z",
          decision_id: 9,
          decision_kind: "push_gate",
          options: ["approve", "send_back", "retry"],
        },
      },
    ],
  };
}

async function render(overrides = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(RunView, {
    target,
    props: {
      run: gatedRun(),
      view: {
        engine_tier: "live",
        now: "2026-08-22T11:30:00Z",
        snapshot_age_seconds: 0,
      },
      ...overrides,
    },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

afterEach(async () => {
  vi.restoreAllMocks();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

function workRun(implementAttempts, extra = {}) {
  return {
    workflow_id: "wf/work",
    dbos_status: "PENDING",
    state: "running",
    task: { text: "Do the work" },
    created_at: "2026-08-22T11:00:00Z",
    updated_at: "2026-08-22T11:30:00Z",
    completed_at: null,
    cost_usd: 0,
    plan: {
      pinned: true,
      max_attempts: 3,
      max_review_cycles: 2,
      turn_timeout_seconds: 1800,
    },
    nodes: [
      {
        key: "implement",
        kind: "work",
        label: "implement",
        state: "done",
        model: "luna",
        deps: [],
        attempts: implementAttempts,
      },
      {
        key: "review",
        kind: "work",
        label: "review",
        state: "running",
        model: "opus",
        deps: ["implement"],
        attempts: [],
      },
    ],
    ...extra,
  };
}

function attemptAt(n, state = "done") {
  return {
    n,
    session_id: 300 + n,
    model: "luna",
    state,
    started_at: "2026-08-22T11:05:00Z",
    ended_at: state === "running" ? null : "2026-08-22T11:08:00Z",
    cost_usd: 0.1,
    events: [],
    live: null,
    rationale: { parse_status: "parsed", paths: [], deviations: [] },
  };
}

describe("attempt ordinals", () => {
  // An attempt increments ONLY when the branch head did not move, so a single
  // attempt is the healthy path for every node. Printing "1 attempt" there
  // reads as an iteration budget the engine does not have.
  test("a single attempt reports state and duration, never an ordinal", async () => {
    const target = await render({ run: workRun([attemptAt(1)]) });
    expect(target.textContent).not.toContain("1 attempt");
    expect(target.textContent).not.toContain("attempt 1");
  });

  test("a retried node still reports how many times it ran", async () => {
    const target = await render({
      run: workRun([attemptAt(1, "failed"), attemptAt(2)]),
    });
    expect(target.textContent).toContain("2 attempts");
    expect(target.textContent).toContain("attempt 2");
  });

  // max_review_cycles is a plan constant, not run state: it said the same
  // thing on every run whether the reviewer had sent anything back or not.
  test("the review node never advertises its cycle ceiling", async () => {
    const target = await render({ run: workRun([attemptAt(1)]) });
    expect(target.textContent).not.toContain("up to 2 cycles");
  });
});

describe("run decision block", () => {
  test("renders options and posts one decision while disabling every option", async () => {
    let resolveFetch;
    global.fetch = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const target = await render();
    const buttons = [...target.querySelectorAll(".decide .btn")];
    const note = target.querySelector(".decide .note");

    expect(buttons.map((button) => button.textContent.trim())).toEqual([
      "Approve and push",
      "Send back",
      "Retry",
    ]);
    expect(target.textContent).toContain("Waiting on you");
    expect(target.textContent).toContain("waiting 15m");
    expect(note.maxLength).toBe(2000);
    note.value = "looks good";
    note.dispatchEvent(new Event("input", { bubbles: true }));
    buttons[0].click();
    await tick();

    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledWith(
      "/agents/runs/wf%2Fgated/nodes/push_gate/decision",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ decision: "approve", note: "looks good" }),
      }),
    );
    expect(buttons.every((button) => button.disabled)).toBe(true);

    resolveFetch(
      new Response(
        JSON.stringify({ decision: "approve", actor_subject: "joe" }),
        { status: 200 },
      ),
    );
    await vi.waitFor(() =>
      expect(
        target.querySelector(".decision-submitted")?.textContent,
      ).toContain("decided Approve and push by joe"),
    );
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });

  test("surfaces a 409 detail through the page error callback", async () => {
    global.fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "no open decision" }), {
          status: 409,
        }),
    );
    const onError = vi.fn();
    const target = await render({ onError });

    target.querySelector(".decide .btn").click();

    await vi.waitFor(() =>
      expect(onError).toHaveBeenCalledWith("no open decision"),
    );
  });

  test("keeps fixture decisions local to the design surface", async () => {
    global.fetch = vi.fn();
    const run = gatedRun();
    run.workflow_id = "fixture-gated";
    const target = await render({ run });

    target.querySelector(".decide .btn").click();
    await tick();

    expect(global.fetch).not.toHaveBeenCalled();
    expect(target.querySelector(".decision-submitted")?.textContent).toContain(
      "decided Approve and push by you",
    );
  });

  test("keeps local decision state across polls and resets it for a new run", async () => {
    global.fetch = vi.fn(() => new Promise(() => {}));
    const target = document.createElement("div");
    document.body.append(target);
    const component = createClassComponent({
      component: RunView,
      target,
      props: {
        run: gatedRun(),
        view: {
          engine_tier: "live",
          now: "2026-08-22T11:30:00Z",
          snapshot_age_seconds: 0,
        },
      },
    });

    try {
      const note = target.querySelector(".decide .note");
      note.value = "keep this note";
      note.dispatchEvent(new Event("input", { bubbles: true }));
      target.querySelector(".decide .btn").click();
      await tick();

      component.$set({
        run: { ...gatedRun(), updated_at: "2026-08-22T11:32:00Z" },
      });
      await tick();

      expect(target.querySelector(".decide .note").value).toBe(
        "keep this note",
      );
      expect(
        [...target.querySelectorAll(".decide .btn")].every(
          (button) => button.disabled,
        ),
      ).toBe(true);

      component.$set({
        run: { ...gatedRun(), workflow_id: "wf/fresh" },
      });
      await tick();

      expect(target.querySelector(".decide .note").value).toBe("");
      expect(
        [...target.querySelectorAll(".decide .btn")].every(
          (button) => !button.disabled,
        ),
      ).toBe(true);
    } finally {
      component.$destroy();
      target.remove();
    }
  });

  test("renders the durable decision record and its human note", async () => {
    const run = gatedRun();
    const decidedAt = "2026-08-22T11:30:00Z";
    run.state = "approved";
    run.dbos_status = "SUCCESS";
    run.disposition = null;
    run.nodes[0] = {
      ...run.nodes[0],
      state: "passed",
      blocked_on: null,
      evidence: { kind: "branch_head", summary: "head abc123 was pushed" },
      decision_record: {
        node_key: "push_gate",
        kind: "push_gate",
        decision: "approve",
        decision_note: "ship after lunch",
        actor_subject: "joe",
        decided_at: decidedAt,
        decision_id: 9,
        ask: "Approve this branch for push?",
      },
    };

    const target = await render({ run });

    expect(target.querySelector(".decision-record")?.textContent).toContain(
      `decided Approve and push by joe · ${clockTime(decidedAt)}`,
    );
    expect(target.querySelector(".decision-record-note")?.textContent).toBe(
      "ship after lunch",
    );
  });

  test("renders an expired decision window without a missing actor", async () => {
    const run = gatedRun();
    run.state = "escalated";
    run.dbos_status = "SUCCESS";
    run.disposition = null;
    run.nodes[0] = {
      ...run.nodes[0],
      state: "escalated",
      blocked_on: null,
      decision_record: {
        node_key: "push_gate",
        kind: "push_gate",
        decision: "expired",
        decision_note: null,
        actor_subject: null,
        decided_at: "2026-08-22T11:30:00Z",
        decision_id: 9,
        ask: "Approve this branch for push?",
      },
    };

    const target = await render({ run });

    expect(target.querySelector(".decision-record")?.textContent).toContain(
      "the decision window expired",
    );
    expect(target.querySelector(".decision-record")?.textContent).not.toContain(
      "decided expired by",
    );
  });
});
