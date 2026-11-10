import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, unmount, tick } from "svelte";
import Harness from "./Harness.svelte";
import {
  reviewData,
  notesData,
  grimoireData,
  chatEvents,
  qwenEvents,
  stats,
  sse,
  qwenSession,
  noteDetail,
} from "./fixtures.js";
import { navigationCalls } from "./navigation.js";
let instance;
const text = () => document.body.textContent;
const button = (label) =>
  [...document.querySelectorAll("button")].find((element) =>
    element.textContent.includes(label),
  );
async function show(view, data, form) {
  instance = mount(Harness, {
    target: document.body,
    props: { view, data, form },
  });
  await tick();
}
async function enter(selector, value) {
  const element = document.querySelector(selector);
  element.value = value;
  element.dispatchEvent(new Event("input", { bubbles: true }));
  await tick();
}
beforeEach(() => {
  localStorage.clear();
  navigationCalls.length = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => Response.json({ type: "success", status: 200 })),
  );
  // happy-dom has no rasterizer. DOM checks exercise the real graph component
  // with only its Canvas2D platform replaced; Chromium checks paint it for real.
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    new Proxy(
      { measureText: () => ({ width: 60 }) },
      { get: (object, key) => object[key] ?? (() => {}) },
    ),
  );
});
afterEach(async () => {
  if (instance) await unmount(instance);
  instance = null;
  document.body.innerHTML = "";
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("review real page", () => {
  it("renders the queue, preserves tab/mode query navigation, and switches scheme", async () => {
    await show("review");
    expect(text()).toContain("Synthetic context");
    button("Notes").click();
    await tick();
    expect(navigationCalls[0].url).toContain("/review?tab=notes");
    button("Audit auto-decisions").click();
    await tick();
    expect(navigationCalls[1].url).toContain("mode=audit");
    document.querySelector("button.scheme").click();
    await tick();
    expect(localStorage.getItem("td-theme")).toMatch(/light|dark/);
  });
  it("submits a decision and removes the processed item", async () => {
    await show("review");
    button("Keep").click();
    await vi.waitFor(() => expect(text()).toContain("Second synthetic gap"));
    expect(fetch.mock.calls[0][0]).toBe("?/decide");
    expect(fetch.mock.calls[0][1].body.get("path")).toBe(
      "/api/knowledge/gaps/fixture-gap/verify",
    );
  });
  it("deletes and offers undo using the existing action contract", async () => {
    await show("review", { ...reviewData, mode: "audit" });
    button("Delete").click();
    await tick();
    await vi.waitFor(() => expect(text()).toContain("Undo (u)"));
    button("Undo (u)").click();
    await vi.waitFor(() =>
      expect(
        fetch.mock.calls.some(([path]) => path === "?/undeleteAction"),
      ).toBe(true),
    );
  });
  it("rolls back a rejected request and shows an error", async () => {
    fetch.mockRejectedValue(new Error("Synthetic rejection"));
    await show("review");
    button("Keep").click();
    await vi.waitFor(() =>
      expect(document.querySelector('[role="alert"]').textContent).toContain(
        "Synthetic rejection",
      ),
    );
    expect(text()).toContain("Synthetic context");
  });
  it("renders an empty queue", async () => {
    await show("review", { ...reviewData, items: [] });
    expect(text()).toContain("Queue empty");
  });
  it("renders a loader error", async () => {
    await show("review", {
      ...reviewData,
      items: [],
      error: "Synthetic loader failure",
    });
    expect(text()).toContain("Synthetic loader failure");
  });
});
describe("chat real page", () => {
  it("renders empty, sends the existing payload and receives a stream and graph node", async () => {
    fetch.mockResolvedValue(new Response(sse(chatEvents)));
    await show("chat");
    expect(text()).toContain("Ask a question");
    await enter(".chat-input-bar input", "Synthetic question");
    document.querySelector('[aria-label="Send message"]').click();
    await vi.waitFor(() =>
      expect(text()).toContain("Synthetic streamed answer"),
    );
    expect(fetch.mock.calls[0][0]).toBe("/private/chat");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      message: "Synthetic question",
      history: [],
    });
    await vi.waitFor(() =>
      expect(
        document.querySelector('.graph-hit-areas [role="button"]'),
      ).not.toBeNull(),
    );
  });
  it("shows connection errors and re-enables send", async () => {
    fetch.mockRejectedValue(new Error("Synthetic offline"));
    await show("chat");
    await enter(".chat-input-bar input", "question");
    document.querySelector('[aria-label="Send message"]').click();
    await vi.waitFor(() => expect(text()).toContain("Synthetic offline"));
    expect(document.querySelector(".chat-input-bar input").disabled).toBe(
      false,
    );
  });
});
describe("notes real page", () => {
  async function selectNote() {
    const canvas = document.querySelector("canvas");
    canvas.dispatchEvent(
      new MouseEvent("mousedown", { clientX: 0, clientY: 0, bubbles: true }),
    );
    window.dispatchEvent(new MouseEvent("mouseup", { clientX: 0, clientY: 0 }));
    await tick();
  }
  it("mounts the graph, searches and toggles clusters with keyboard", async () => {
    await show("notes");
    expect(document.querySelector("canvas")).not.toBeNull();
    expect(text()).toContain("1 NOTES");
    await enter("#graph-search-input", "Synthetic");
    expect(document.querySelector("#graph-search-input").value).toBe(
      "Synthetic",
    );
    const cluster = document.querySelector('[aria-pressed="true"]');
    cluster.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Enter", bubbles: true }),
    );
    await tick();
    expect(cluster.getAttribute("aria-pressed")).toBe("false");
  });
  it("renders an empty graph without dropping search or home", async () => {
    await show("notes", {
      graph: { ...notesData.graph, nodes: [], edges: [] },
    });
    expect(text()).toContain("0 NOTES");
    expect(document.querySelector('a[href="/"]')).not.toBeNull();
    expect(document.querySelector("#graph-search-input")).not.toBeNull();
  });
  it("loads selected private note details and closes the panel", async () => {
    fetch.mockResolvedValue(Response.json(noteDetail));
    await show("notes");
    await selectNote();
    await vi.waitFor(() => expect(text()).toContain("Review history (0)"));
    expect(fetch.mock.calls[0][0]).toBe(
      "/api/knowledge/notes/fixture-note?include_review_history=true",
    );
    document.querySelector('[aria-label="close"]').click();
    await tick();
    expect(document.querySelector(".panel")).toBeNull();
  });
  it("shows a note detail failure", async () => {
    fetch.mockRejectedValue(new Error("Synthetic note unavailable"));
    await show("notes");
    await selectNote();
    await vi.waitFor(() => expect(text()).toContain("couldn't load note body"));
  });
});
describe("Qwen real page", () => {
  it("starts empty, streams reasoning/answer, persists sessions and shows stats", async () => {
    fetch.mockImplementation(async (path) =>
      path.endsWith("stats")
        ? Response.json(stats)
        : new Response(sse(qwenEvents)),
    );
    await show("qwen");
    expect(text()).toContain("Ask something");
    await enter("textarea", "Synthetic prompt");
    button("Send").click();
    await vi.waitFor(() =>
      expect(text()).toContain("Synthetic streamed Qwen answer"),
    );
    await vi.waitFor(() => expect(text()).toContain("synthetic-model"));
    expect(text()).toContain("Synthetic reasoning");
    const [, options] = fetch.mock.calls.find(([path]) =>
      path.endsWith("/chat"),
    );
    expect(JSON.parse(options.body)).toEqual({
      messages: [{ role: "user", content: "Synthetic prompt" }],
      enableThinking: true,
      maxTokens: 8192,
    });
    expect(localStorage.getItem("qwen-chat-sessions-v1")).toContain(
      "Synthetic streamed Qwen answer",
    );
    button("+ New chat").click();
    await tick();
    expect(text()).toContain("Ask something");
    expect(document.querySelectorAll(".sessions li")).toHaveLength(2);
    document.querySelector(".sessions .drop").click();
    await tick();
    expect(document.querySelectorAll(".sessions li")).toHaveLength(1);
  });
  it("restores populated sessions and polls stats every five seconds, cleaning up", async () => {
    vi.useFakeTimers();
    localStorage.setItem(
      "qwen-chat-sessions-v1",
      JSON.stringify([qwenSession]),
    );
    fetch.mockResolvedValue(Response.json(stats));
    await show("qwen");
    expect(text()).toContain("Synthetic answer");
    expect(fetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(2);
    await unmount(instance);
    instance = null;
    await vi.advanceTimersByTimeAsync(5000);
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it("shows both stats and stream errors", async () => {
    fetch.mockRejectedValue(new Error("Synthetic unavailable"));
    await show("qwen");
    await enter("textarea", "question");
    button("Send").click();
    await vi.waitFor(() =>
      expect(document.querySelectorAll(".err").length).toBe(2),
    );
    expect(text()).toContain("Synthetic unavailable");
  });
});
describe("Grimoire real page", () => {
  it("renders player draft forms with the existing names, IDs and submit action", async () => {
    await show("grimoire");
    const save = document.querySelector('form[action="?/save"]');
    expect(new FormData(save).get("version_id")).toBe("fixture-version");
    expect(new FormData(save).get("strength")).toBe("12");
    await enter('input[name="ancestry"]', "Synthetic elf");
    expect(new FormData(save).get("ancestry")).toBe("Synthetic elf");
    expect(document.querySelector('form[action="?/submit"]')).not.toBeNull();
    expect(document.querySelector('form[action="?/decide"]')).toBeNull();
    document.querySelector(".history summary").click();
    await tick();
    expect(document.querySelector(".history").open).toBe(true);
  });
  it("limits DM decisions to submitted versions", async () => {
    await show("grimoire", grimoireData("dm", "submitted"));
    const decide = document.querySelector('form[action="?/decide"]');
    expect(new FormData(decide).get("version_id")).toBe("fixture-version");
    expect(
      [...decide.querySelectorAll('button[name="decision"]')].map(
        (element) => element.value,
      ),
    ).toEqual(["return", "approve"]);
    expect(document.querySelector('form[action="?/save"]')).toBeNull();
  });
  it("renders no campaigns", async () => {
    await show("grimoire", { groups: [] });
    expect(text()).toContain("No campaigns yet");
  });
  it("renders unavailable and action errors", async () => {
    await show(
      "grimoire",
      { unavailable: true, message: "Synthetic loader unavailable" },
      { error: "Synthetic form rejection" },
    );
    expect(text()).toContain("Sheets are unavailable");
    expect(document.querySelector('[role="alert"]').textContent).toContain(
      "Synthetic form rejection",
    );
  });
});
