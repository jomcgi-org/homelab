// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import Page from "./+page.svelte";

const mounted = [];

function escalation(overrides = {}) {
  return {
    receipt_id: 3,
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

  test("clicking an option posts it and re-reads the list", async () => {
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
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    target.querySelector(".option").click();
    await settle();

    expect(fetchMock.mock.calls[0][0]).toBe("/agents/escalations/decisions/3");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "split",
    });
    expect(fetchMock.mock.calls[1][0]).toBe("/agents/escalations");
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
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    target.querySelector(".option").click();
    await settle();

    expect(target.querySelector("[role=alert]").textContent).toContain(
      "already decided as close",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("a number key picks the option in that position", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    vi.stubGlobal("fetch", fetchMock);
    renderPage({ escalations: [escalation()], error: false });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "2" }));
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "hold",
    });
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
    vi.stubGlobal("fetch", fetchMock);
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

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      action: "chat",
      note: "Does this cover the friends tier?",
    });
  });

  test("typing in the note box never triggers a hotkey", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
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
    vi.stubGlobal("fetch", fetchMock);
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
    expect(fetchMock.mock.calls[0][0]).toBe("/agents/escalations/decisions/3");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "escape:close",
    });
  });

  test("Escape cancels an armed close before it dismisses anything", async () => {
    const fetchMock = acceptingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "x" }));
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await tick();
    expect(target.querySelector(".confirm")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await settle();
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "escape:dismiss",
    });
  });

  test("defer sends at once and carries the card's own note", async () => {
    const fetchMock = acceptingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    const box = target.querySelector("textarea");
    box.value = "after the hub migration";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "d" }));
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "escape:defer",
      note: "after the hub migration",
    });
  });

  test("arming with the mouse moves the cursor, so a key cannot close another card", async () => {
    const fetchMock = acceptingFetch();
    vi.stubGlobal("fetch", fetchMock);
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

    expect(fetchMock.mock.calls[0][0]).toBe("/agents/escalations/decisions/4");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "escape:close",
    });
  });

  test("clicking a brief option on another card moves the cursor to it", async () => {
    const fetchMock = acceptingFetch();
    vi.stubGlobal("fetch", fetchMock);
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
    expect(fetchMock.mock.calls[0][0]).toBe("/agents/escalations/decisions/4");
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
    vi.stubGlobal("fetch", fetchMock);
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

    expect(fetchMock.mock.calls[0][0]).toBe("/agents/escalations/decisions/4");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "split",
    });
  });

  test("the focused card's own note is sent with its decision", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ok: true }) })
      .mockResolvedValue({ ok: true, json: async () => ({ escalations: [] }) });
    vi.stubGlobal("fetch", fetchMock);
    const target = renderPage({ escalations: [escalation()], error: false });

    const box = target.querySelector("textarea");
    box.value = "ship the console half";
    box.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();
    target.querySelector(".option").click();
    await settle();

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      option_key: "split",
      note: "ship the console half",
    });
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
    vi.stubGlobal("fetch", fetchMock);
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
    vi.stubGlobal("fetch", fetchMock);
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
