// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

const mounted = [];
const DECISION_ID = `decision:${"a".repeat(64)}`;

function durableBody(body, decisionId = DECISION_ID) {
  return {
    ...body,
    decision_id: decisionId,
    request_key: expect.stringMatching(/^browser-v1:[0-9a-f]{64}$/),
  };
}

function escalation(overrides = {}) {
  return {
    receipt_id: 3,
    decision_id: DECISION_ID,
    issue_number: 6002,
    title: "escalations are decisions, not messages",
    url: "https://github.com/jomcgi-org/homelab/issues/6002",
    task_class: "refine",
    state: "succeeded",
    recommendation: "split",
    question: "Should the console ship before the API?",
    summary: "Two features are wearing one issue number.",
    comment_url: "https://github.com/jomcgi-org/homelab/issues/6002#c1",
    downgraded: false,
    options: [
      {
        key: "split",
        label: "Split the console out of the API",
        effect: "split",
        children: 2,
      },
      { key: "hold", label: "Leave it open", effect: "hold", children: 0 },
    ],
    escape: [
      { key: "escape:close", label: "Close the issue", effect: "escape-close" },
      { key: "escape:defer", label: "Defer it", effect: "escape-defer" },
      {
        key: "escape:dismiss",
        label: "Dismiss the escalation",
        effect: "escape-dismiss",
      },
    ],
    chat: [],
    resolved: null,
    open: true,
    briefing: false,
    ...overrides,
  };
}

function contextDocument(receiptId = 3) {
  return {
    receipt_id: receiptId,
    ask: {
      line: `#${receiptId} ship the context band`,
      issue_number: receiptId,
      url: `https://github.com/example/repo/issues/${receiptId}`,
      task_class: "refine",
      generation: 2,
      body_head: "Show the operator the evidence behind each claim.",
    },
    stopped: {
      line: "waiting for an operator decision",
      kind: "advisory",
      recommendation: "split",
      question: "Should this ship?",
      summary: "The boundary needs a decision.",
      reason: "The choice changes scope.",
    },
    happened: {
      line: "2 attempts, last review revise",
      attempts: 2,
      last_review: { verdict: "revise", summary: "Add context." },
      pr: {
        number: 77,
        url: "https://github.com/example/repo/pull/77",
        state: "open",
      },
      branch: "factory/context-band",
    },
    cost: {
      line: "$1.25 of $4.00 across 1 task",
      committed: 1.25,
      ceiling: 4,
      num_tasks: 1,
    },
    lineage: {
      line: "child of #6000",
      parent: { id: 10, github_issue_number: 6000, title: "Parent work" },
      children: [],
      blocked_by: [],
      blocks: [],
      superseded_by: null,
      supersedes: [],
      prior_receipts: [],
    },
  };
}

function contextResponse(receiptId = 3) {
  return { ok: true, json: async () => contextDocument(receiptId) };
}

function stubFetch(
  fetchMock,
  contextFetch = vi.fn().mockResolvedValue(contextResponse()),
) {
  vi.stubGlobal(
    "fetch",
    vi.fn((url, init) =>
      /^\/factory\/escalations\/context\/\d+$/.test(url)
        ? contextFetch(url, init)
        : fetchMock(url, init),
    ),
  );
  return contextFetch;
}

/** A fetch that accepts the decision, then returns an empty list. */
function acceptingFetch() {
  return vi
    .fn()
    .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
    .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
}

/**
 * Let the decision round trip finish. It awaits two fetches and their json
 * bodies before the list re-renders, so a microtask tick alone lands mid
 * flight and reads the DOM from before the update.
 */
async function settle() {
  for (let i = 0; i < 4; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    await tick();
  }
}

function renderPage(data) {
  if (!vi.isMockFunction(globalThis.fetch)) stubFetch(vi.fn());
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

describe("escalations page", () => {
  test("renders a delivery escalation beside a refine one", () => {
    // A paused delivery planner leaves the lane as a card on this page. It
    // carries a branch and usually a pull request, which a refine escalation
    // never has, and the page has to say which kind it is looking at.
    const delivery = escalation({
      receipt_id: 9,
      issue_number: 3824,
      title: "the scope covers two surfaces",
      task_class: "bug-fix",
      kind: "delivery",
      state: "escalated",
      recommendation: "deliver",
      branch: "factory/t-d49f3323",
      pr_url: "https://github.com/jomcgi-org/homelab/pull/6012",
      options: [
        {
          key: "continue-narrowed",
          label: "Deliver only the /invoke path",
          effect: "agent-ready",
          children: 0,
        },
        { key: "hold", label: "Leave it open", effect: "hold", children: 0 },
      ],
    });
    const target = renderPage({
      escalations: [delivery, escalation({ kind: "advisory" })],
      error: false,
    });

    const panels = [...target.querySelectorAll(".panel")];
    expect(panels).toHaveLength(2);
    expect(panels[0].querySelector(".panel-head").textContent).toContain(
      "left the lane",
    );
    expect(panels[1].querySelector(".panel-head").textContent).not.toContain(
      "left the lane",
    );
    const links = panels[0].querySelector(".links");
    expect(links.textContent).toContain("the decision card");
    expect(links.textContent).toContain("factory/t-d49f3323");
    expect(
      [...links.querySelectorAll("a")].map((a) => a.getAttribute("href")),
    ).toContain("https://github.com/jomcgi-org/homelab/pull/6012");
    expect(panels[1].querySelector(".links").textContent).toContain(
      "the brief",
    );
    // The buttons are live: an escalated receipt runs nothing.
    expect(panels[0].querySelector(".option:not(.chat-button)").disabled).toBe(
      false,
    );
  });

  test("renders the question, the outcome, and the options in order", () => {
    const target = renderPage({ escalations: [escalation()], error: false });

    expect(target.querySelector(".outcome").textContent).toContain(
      "Two features are wearing one issue number.",
    );
    expect(target.querySelector(".question").textContent).toContain(
      "Should the console ship before the API?",
    );
    const options = [...target.querySelectorAll(".option:not(.chat-button)")];
    expect(options).toHaveLength(2);
    expect(options[0].classList.contains("primary")).toBe(true);
    expect(options[0].textContent).toContain(
      "Split the console out of the API",
    );
    expect(options[0].textContent).toContain("opens 2 issues");
    expect(options[1].classList.contains("primary")).toBe(false);
    expect(target.querySelector(".panel-head").textContent).toContain(
      "recommend split",
    );
  });

  test("says so when nothing is waiting", () => {
    const target = renderPage({ escalations: [], error: false });
    expect(target.querySelector(".none").textContent).toContain(
      "Nothing is waiting",
    );
    expect(target.querySelector(".stats").textContent).toContain("0");
  });

  test("clicking an option posts its brief identity and re-reads the list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({
        ok: true,
        json: async () => ({
          escalations: [
            escalation({
              open: false,
              resolved: {
                option_key: "split",
                label: "Split the console out of the API",
                actor: "joe@example.test",
                decided_at: new Date().toISOString(),
              },
            }),
          ],
        }),
      });
    stubFetch(fetchMock);
    const target = renderPage({
      escalations: [escalation({ decision_id: "decision:reviewed" })],
      error: false,
    });

    target.querySelector(".option").click();
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe("/factory/escalations/decisions/3");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({ option_key: "split" }, "decision:reviewed"),
    );
    expect(fetchMock.mock.calls[1][0]).toBe("/factory/escalations");
    expect(target.querySelector(".none")).not.toBeNull();
    expect(target.querySelector(".ledger").textContent).toContain(
      "Split the console out of the API by joe@example.test",
    );
  });

  test("a refused decision shows the backend reason and applies nothing", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: "already decided as close" }),
    });
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    target.querySelector(".option").click();
    await settle();

    expect(target.querySelector("[role=alert]").textContent).toContain(
      "already decided as close",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("a retry after a refusal uses a fresh durable request key", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        status: 409,
        json: async () => ({
          detail: "a brief is running on this issue; decide when it settles",
        }),
      })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    target.querySelector(".option").click();
    await settle();
    const first = JSON.parse(fetchMock.mock.calls[0][1].body);

    target.querySelector(".option").click();
    await settle();
    const retry = JSON.parse(fetchMock.mock.calls[1][1].body);

    expect(retry.option_key).toBe(first.option_key);
    expect(retry.decision_id).toBe(first.decision_id);
    expect(retry.request_key).not.toBe(first.request_key);
    expect(fetchMock.mock.calls[2][0]).toBe("/factory/escalations");
  });

  test("an uncertain outcome refreshes the escalation list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          ok: false,
          state: "outcome_unknown",
          reason: "GitHub effects may have occurred; inspect the issue",
        }),
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({
          escalations: [escalation({ summary: "Fresh server state." })],
        }),
      });
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    target.querySelector(".option").click();
    await settle();

    expect(fetchMock.mock.calls[1][0]).toBe("/factory/escalations");
    expect(target.querySelector(".outcome").textContent).toContain(
      "Fresh server state.",
    );
    expect(target.querySelector("[role=alert]").textContent).toContain(
      "GitHub effects may have occurred",
    );
  });

  test("a card without an exact identity cannot reach the mutation proxy", async () => {
    const fetchMock = vi.fn();
    stubFetch(fetchMock);
    const target = renderPage({
      escalations: [escalation({ decision_id: null })],
      error: false,
    });

    target.querySelector(".option").click();
    await settle();

    expect(target.querySelector("[role=alert]").textContent).toContain(
      "no exact identity",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  test("a number key picks the option in that position", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    stubFetch(fetchMock);
    renderPage({ escalations: [escalation()], error: false });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "2" }));
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({ option_key: "hold" }),
    );
  });

  test("j and k move the cursor without wrapping", async () => {
    const target = renderPage({
      escalations: [
        escalation(),
        escalation({ receipt_id: 4, issue_number: 6003 }),
      ],
      error: false,
    });

    expect(
      target.querySelectorAll(".panel")[0].classList.contains("here"),
    ).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    await tick();
    expect(
      target.querySelectorAll(".panel")[1].classList.contains("here"),
    ).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    await tick();
    expect(
      target.querySelectorAll(".panel")[1].classList.contains("here"),
    ).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "k" }));
    await tick();
    expect(
      target.querySelectorAll(".panel")[0].classList.contains("here"),
    ).toBe(true);
  });

  test("chat refuses an empty note and sends one that is filled in", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    target.querySelector(".chat-button").click();
    await settle();
    expect(target.querySelector("[role=alert]").textContent).toContain(
      "needs a note",
    );
    expect(fetchMock).not.toHaveBeenCalled();

    const box = target.querySelector("textarea");
    box.value = "Does this cover the friends tier?";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    target.querySelector(".chat-button").click();
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({
        action: "chat",
        note: "Does this cover the friends tier?",
      }),
    );
  });

  test("typing in the note box never triggers a hotkey", async () => {
    const fetchMock = vi.fn();
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    const box = target.querySelector("textarea");
    // The escape keys are ordinary letters, so a note mentioning a deadline
    // or an index would fire them if the guard did not cover them too.
    for (const key of ["1", "x", "d", "Escape"]) {
      box.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
    }
    await settle();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(target.querySelector(".confirm")).toBeNull();
  });

  test("every card carries the escape row below the brief's own options", () => {
    const target = renderPage({ escalations: [escalation()], error: false });
    const ways = [...target.querySelectorAll(".escape-btn")];
    expect(
      ways.map((button) => button.querySelector(".hotkey").textContent.trim()),
    ).toEqual(["x", "d", "Esc"]);
    expect(ways[0].textContent).toContain("Close the issue");
    expect(ways[2].textContent).toContain("keeps needs-human");
    // The brief's options are untouched, so 1 to 4 still mean what it said.
    expect(target.querySelectorAll(".option:not(.chat-button)")).toHaveLength(
      2,
    );
  });

  test("the close arms once and sends on the second press", async () => {
    const fetchMock = acceptingFetch();
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "x" }));
    await tick();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(target.querySelector(".confirm").textContent).toContain(
      "Close #6002 as not planned",
    );
    expect(target.querySelector(".escape-btn").textContent).toContain(
      "Confirm close",
    );

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "x" }));
    await settle();
    expect(fetchMock.mock.calls[0][0]).toBe("/factory/escalations/decisions/3");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({ option_key: "escape:close" }),
    );
  });

  test("Escape cancels an armed close before it dismisses anything", async () => {
    const fetchMock = acceptingFetch();
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "x" }));
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await tick();
    expect(target.querySelector(".confirm")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await settle();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({ option_key: "escape:dismiss" }),
    );
  });

  test("defer sends at once and carries the card's own note", async () => {
    const fetchMock = acceptingFetch();
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    const box = target.querySelector("textarea");
    box.value = "after the hub migration";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "d" }));
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({
        option_key: "escape:defer",
        note: "after the hub migration",
      }),
    );
  });

  test("arming with the mouse moves the cursor, so a key cannot close another card", async () => {
    const fetchMock = acceptingFetch();
    stubFetch(fetchMock);
    const target = renderPage({
      escalations: [
        escalation(),
        escalation({ receipt_id: 4, issue_number: 6003 }),
      ],
      error: false,
    });

    // Arm the close on the SECOND card with the mouse while the cursor is
    // still on the first, then finish it from the keyboard.
    const second = target.querySelectorAll(".panel")[1];
    second.querySelector(".escape-btn").click();
    await tick();
    expect(
      target.querySelectorAll(".panel")[1].classList.contains("here"),
    ).toBe(true);
    expect(target.querySelector(".confirm").textContent).toContain("#6003");

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "x" }));
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe("/factory/escalations/decisions/4");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({ option_key: "escape:close" }),
    );
  });

  test("clicking a brief option on another card moves the cursor to it", async () => {
    const fetchMock = acceptingFetch();
    stubFetch(fetchMock);
    const target = renderPage({
      escalations: [
        escalation(),
        escalation({ receipt_id: 4, issue_number: 6003 }),
      ],
      error: false,
    });

    const second = target.querySelectorAll(".panel")[1];
    second.querySelector(".option").click();
    await tick();
    expect(
      target.querySelectorAll(".panel")[1].classList.contains("here"),
    ).toBe(true);
    await settle();
    expect(fetchMock.mock.calls[0][0]).toBe("/factory/escalations/decisions/4");
  });

  test("a briefing card offers the escapes disabled rather than clickable", () => {
    const target = renderPage({
      escalations: [escalation({ briefing: true })],
      error: false,
    });
    const ways = [...target.querySelectorAll(".escape-btn")];
    expect(ways).toHaveLength(3);
    expect(ways.every((button) => button.disabled)).toBe(true);
  });

  test("a decided card offers no way out, because there is nothing to leave", () => {
    const target = renderPage({
      escalations: [
        escalation({
          open: false,
          escape: [],
          resolved: {
            option_key: "escape:dismiss",
            label: "Dismiss the escalation",
            actor: "joe@example.test",
            decided_at: new Date().toISOString(),
          },
        }),
      ],
      error: false,
    });
    expect(target.querySelector(".escape-btn")).toBeNull();
    expect(target.querySelector(".ledger").textContent).toContain(
      "Dismiss the escalation by joe@example.test",
    );
  });

  test("an unavailable board says so rather than rendering an empty list", () => {
    const target = renderPage({ escalations: [], error: true });
    expect(target.querySelector(".warn-line").textContent).toContain(
      "board is unavailable",
    );
  });
});

describe("escalations page, per card state", () => {
  test("a note typed on one card never rides along with another's decision", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    stubFetch(fetchMock);
    const target = renderPage({
      escalations: [
        escalation(),
        escalation({ receipt_id: 4, issue_number: 6003 }),
      ],
      error: false,
    });

    // Type on the first card, then move the cursor and decide on the second.
    const box = target.querySelector("textarea");
    box.value = "only about 6002";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "1" }));
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe("/factory/escalations/decisions/4");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({ option_key: "split" }),
    );
  });

  test("the focused card's own note is sent with its decision", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    const box = target.querySelector("textarea");
    box.value = "ship the console half";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    target.querySelector(".option").click();
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(
      durableBody({
        option_key: "split",
        note: "ship the console half",
      }),
    );
  });

  test("a chat the lane could not queue says so instead of reading as done", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({
          ok: true,
          requeued: false,
          blocked_by: "intake is off",
        }),
      })
      .mockResolvedValue({
        ok: true,
        json: async () => ({
          escalations: [
            escalation({
              chat: [
                {
                  note: "Which tier?",
                  asked_at: new Date().toISOString(),
                  requeued: false,
                  blocked_by: "intake is off",
                },
              ],
            }),
          ],
        }),
      });
    stubFetch(fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    const box = target.querySelector("textarea");
    box.value = "Which tier?";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    target.querySelector(".chat-button").click();
    await settle();

    expect(target.querySelector("[role=status]").textContent).toContain(
      "no brief was queued: intake is off",
    );
    expect(target.querySelector(".unqueued").textContent).toContain(
      "intake is off",
    );
  });

  test("a card the lane is briefing offers no buttons", async () => {
    const fetchMock = vi.fn();
    stubFetch(fetchMock);
    const target = renderPage({
      escalations: [escalation({ briefing: true })],
      error: false,
    });

    expect(target.querySelector(".panel-head").textContent).toContain(
      "briefing",
    );
    const first = target.querySelector(".option");
    expect(first.disabled).toBe(true);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "1" }));
    await settle();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(target.querySelector("[role=alert]").textContent).toContain(
      "decide when it settles",
    );
  });
});

describe("escalation context band", () => {
  test("fetches the cursor context once and renders all five lines", async () => {
    const contextFetch = vi
      .fn()
      .mockResolvedValueOnce(contextResponse(3))
      .mockResolvedValue(contextResponse(3));
    stubFetch(vi.fn(), contextFetch);
    const target = renderPage({
      escalations: [escalation({ receipt_id: 3 })],
      error: false,
    });

    await settle();

    expect(contextFetch).toHaveBeenCalledTimes(1);
    expect(contextFetch.mock.calls[0][0]).toBe(
      "/factory/escalations/context/3",
    );
    expect(
      [...target.querySelectorAll(".ctx-row summary")].map((node) =>
        node.textContent.trim(),
      ),
    ).toEqual([
      "#3 ship the context band",
      "waiting for an operator decision",
      "2 attempts, last review revise",
      "$1.25 of $4.00 across 1 task",
      "child of #6000",
    ]);

    await tick();
    await tick();
    expect(contextFetch).toHaveBeenCalledTimes(1);
  });

  test("e opens and closes all five context rows together", async () => {
    stubFetch(vi.fn());
    const target = renderPage({
      escalations: [escalation()],
      error: false,
    });
    await settle();

    const rows = [...target.querySelectorAll(".ctx-row")];
    expect(rows).toHaveLength(5);
    expect(rows.every((row) => !row.open)).toBe(true);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "e" }));
    await tick();
    expect(rows.every((row) => row.open)).toBe(true);

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "e" }));
    await tick();
    expect(rows.every((row) => !row.open)).toBe(true);
  });

  test("j fetches the next context and keeps the first one cached", async () => {
    const contextFetch = vi
      .fn()
      .mockResolvedValueOnce(contextResponse(3))
      .mockResolvedValue(contextResponse(4));
    stubFetch(vi.fn(), contextFetch);
    const target = renderPage({
      escalations: [
        escalation({ receipt_id: 3 }),
        escalation({ receipt_id: 4, issue_number: 6003 }),
      ],
      error: false,
    });
    await settle();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "j" }));
    await settle();

    expect(contextFetch).toHaveBeenCalledTimes(2);
    expect(contextFetch.mock.calls.map(([url]) => url)).toEqual([
      "/factory/escalations/context/3",
      "/factory/escalations/context/4",
    ]);
    const panels = [...target.querySelectorAll(".panel")];
    expect(panels[0].querySelector(".context")).not.toBeNull();
    expect(panels[1].querySelector(".context")).not.toBeNull();
    expect(panels[0].textContent).toContain("#3 ship the context band");
    expect(panels[1].textContent).toContain("#4 ship the context band");
  });

  test("a changed decision identity fetches fresh context", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({
        ok: true,
        json: async () => ({
          escalations: [escalation({ decision_id: "decision:new" })],
        }),
      });
    const contextFetch = vi
      .fn()
      .mockResolvedValueOnce(contextResponse(3))
      .mockResolvedValue(contextResponse(3));
    stubFetch(fetchMock, contextFetch);
    const target = renderPage({
      escalations: [escalation({ decision_id: "decision:old" })],
      error: false,
    });
    await settle();

    target.querySelector(".option").click();
    await settle();

    expect(contextFetch).toHaveBeenCalledTimes(2);
    expect(contextFetch.mock.calls.map(([url]) => url)).toEqual([
      "/factory/escalations/context/3",
      "/factory/escalations/context/3",
    ]);
  });

  test("renders context unavailable for a 502 response", async () => {
    const contextFetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 502,
      json: async () => ({ detail: "context unavailable" }),
    });
    stubFetch(vi.fn(), contextFetch);
    const target = renderPage({ escalations: [escalation()], error: false });

    await settle();

    expect(target.querySelector(".context").textContent).toContain(
      "context unavailable",
    );
  });

  test("never fetches context for a resolved card", async () => {
    const contextFetch = vi.fn().mockResolvedValue(contextResponse());
    stubFetch(vi.fn(), contextFetch);
    renderPage({
      escalations: [
        escalation({
          open: false,
          resolved: {
            option_key: "hold",
            label: "Leave it open",
            actor: "operator@example.test",
            decided_at: new Date().toISOString(),
          },
        }),
      ],
      error: false,
    });

    await settle();

    expect(contextFetch).not.toHaveBeenCalled();
  });
});

describe("escalations page, work item links", () => {
  test("renders a work item link when work_item_id is present", () => {
    const target = renderPage({
      escalations: [escalation({ work_item_id: 42 })],
      error: false,
    });

    const link = target.querySelector('a[href="/factory/work-items/42"]');
    expect(link).not.toBeNull();
    expect(link.textContent).toContain("work item 42");
  });

  test("does not render a work item link when work_item_id is absent", () => {
    const target = renderPage({
      escalations: [escalation()],
      error: false,
    });

    const link = target.querySelector('a[href*="/factory/work-items/"]');
    expect(link).toBeNull();
  });

  test("renders all evidence entries even when labels repeat", async () => {
    const contextFetch = vi.fn().mockResolvedValueOnce({
      ok: true,
      json: async () => ({
        receipt_id: 3,
        ask: {
          line: "#3 test",
          issue_number: 3,
          url: "https://github.com/example/repo/issues/3",
          task_class: "refine",
          generation: 1,
          body_head: "Test body",
        },
        stopped: null,
        happened: { line: "no attempts yet", attempts: 0 },
        cost: null,
        lineage: {
          line: "complex lineage",
          parent: null,
          children: [
            { id: 10, github_issue_number: 1000, title: "Child 1" },
            { id: 11, github_issue_number: 1001, title: "Child 2" },
          ],
          blocked_by: [
            { id: 20, github_issue_number: 2000, title: "Blocker 1" },
          ],
          blocks: [{ id: 21, github_issue_number: 2001, title: "Blocked 1" }],
          superseded_by: [
            { id: 30, github_issue_number: 3000, title: "Superseded 1" },
            { id: 31, github_issue_number: 3001, title: "Superseded 2" },
          ],
          supersedes: [],
          prior_receipts: [
            {
              id: 100,
              generation: 1,
              task_class: "refine",
              state: "succeeded",
            },
            { id: 101, generation: 2, task_class: "refine", state: "failed" },
          ],
        },
      }),
    });
    stubFetch(vi.fn(), contextFetch);
    const target = renderPage({
      escalations: [escalation({ receipt_id: 3 })],
      error: false,
    });

    await settle();

    const lineageRow = [...target.querySelectorAll(".ctx-row")].find((row) =>
      row.textContent.includes("complex lineage"),
    );
    expect(lineageRow).not.toBeNull();

    const evidenceItems = [...lineageRow.querySelectorAll("dt")].map(
      (dt) => dt.textContent,
    );

    expect(evidenceItems).toContain("child");
    expect(evidenceItems).toContain("blocked by");
    expect(evidenceItems).toContain("blocks");
    expect(evidenceItems).toContain("superseded by");
    expect(evidenceItems).toContain("prior receipt");

    const childCount = evidenceItems.filter(
      (label) => label === "child",
    ).length;
    const blockedByCount = evidenceItems.filter(
      (label) => label === "blocked by",
    ).length;
    const blocksCount = evidenceItems.filter(
      (label) => label === "blocks",
    ).length;
    const supersededByCount = evidenceItems.filter(
      (label) => label === "superseded by",
    ).length;
    const priorReceiptCount = evidenceItems.filter(
      (label) => label === "prior receipt",
    ).length;

    expect(childCount).toBe(2);
    expect(blockedByCount).toBe(1);
    expect(blocksCount).toBe(1);
    expect(supersededByCount).toBe(2);
    expect(priorReceiptCount).toBe(2);
  });
});
