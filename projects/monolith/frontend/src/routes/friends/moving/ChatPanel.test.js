// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import ChatPanel from "./ChatPanel.svelte";

let mounted;

function frame(type, data) {
  return `data: ${JSON.stringify({ type, data })}\n\n`;
}

function streamResponse(chunks) {
  const encoder = new TextEncoder();
  const queue = [...chunks];
  return {
    ok: true,
    status: 200,
    body: new ReadableStream({
      pull(controller) {
        if (queue.length === 0) {
          controller.close();
          return;
        }
        controller.enqueue(encoder.encode(queue.shift()));
      },
    }),
  };
}

async function render(inDialog = false) {
  const root = document.createElement(inDialog ? "dialog" : "div");
  const target = inDialog ? document.createElement("div") : root;
  if (inDialog) root.append(target);
  document.body.append(root);
  const component = mount(ChatPanel, { target });
  mounted = { component, root, target };
  await tick();
  return target;
}

async function send(target, text) {
  const input = target.querySelector("input");
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
  target
    .querySelector("form")
    .dispatchEvent(
      new SubmitEvent("submit", { bubbles: true, cancelable: true }),
    );
  await tick();
}

beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  if (!mounted) return;
  await unmount(mounted.component);
  mounted.root.remove();
  mounted = undefined;
});

describe("moving chat panel", () => {
  it("renders a user turn and accumulates assistant tokens", async () => {
    fetch.mockResolvedValue(
      streamResponse([
        frame("token", { text: "Pack " }),
        frame("token", { text: "boxes." }),
        frame("done", {}),
      ]),
    );
    const target = await render();

    await send(target, "What is next?");
    expect(target.textContent).toContain("What is next?");
    await vi.waitFor(() => expect(target.textContent).toContain("Pack boxes."));

    const turns = target.querySelectorAll(".chat-message");
    expect(turns[0].classList.contains("viewer")).toBe(true);
    expect(turns[1].textContent).toContain("Pack boxes.");
    expect(target.querySelector('[role="status"]')?.textContent).toContain(
      "Assistant response complete",
    );
  });

  it("shows a retry-friendly alert", async () => {
    fetch.mockResolvedValue({
      ok: false,
      status: 503,
      json: async () => ({ detail: "Chat is warming up. Please try again." }),
    });
    const target = await render();

    await send(target, "Can you help?");
    await vi.waitFor(() =>
      expect(target.querySelector('[role="alert"]')?.textContent).toContain(
        "Please try again",
      ),
    );
  });

  it("sends both prior turns as history on the second message", async () => {
    fetch
      .mockResolvedValueOnce(
        streamResponse([
          frame("token", { text: "First answer" }),
          frame("done", {}),
        ]),
      )
      .mockResolvedValueOnce(
        streamResponse([
          frame("token", { text: "Second answer" }),
          frame("done", {}),
        ]),
      );
    const target = await render();

    await send(target, "First question");
    await vi.waitFor(() =>
      expect(target.textContent).toContain("First answer"),
    );
    await send(target, "Second question");
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));

    expect(JSON.parse(fetch.mock.calls[1][1].body).history).toEqual([
      { role: "user", content: "First question" },
      { role: "assistant", content: "First answer" },
    ]);
  });

  it("sends at most the last 12 turns as history", async () => {
    fetch.mockImplementation(() =>
      Promise.resolve(
        streamResponse([frame("token", { text: "Answer" }), frame("done", {})]),
      ),
    );
    const target = await render();

    for (let index = 1; index <= 8; index += 1) {
      await send(target, `Question ${index}`);
      await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(index));
      await vi.waitFor(() =>
        expect(target.querySelector("input").disabled).toBe(false),
      );
    }

    const history = JSON.parse(fetch.mock.calls[7][1].body).history;
    expect(history).toHaveLength(12);
    expect(history[0]).toEqual({ role: "user", content: "Question 2" });
    expect(history[11]).toEqual({ role: "assistant", content: "Answer" });
  });

  it("keeps a partial response and clears busy when the dialog closes", async () => {
    const encoder = new TextEncoder();
    fetch.mockImplementation((url, options) =>
      Promise.resolve({
        ok: true,
        status: 200,
        body: new ReadableStream({
          start(controller) {
            controller.enqueue(
              encoder.encode(frame("token", { text: "Partial answer" })),
            );
            options.signal.addEventListener(
              "abort",
              () => controller.error(new DOMException("Aborted", "AbortError")),
              { once: true },
            );
          },
        }),
      }),
    );
    const target = await render(true);

    await send(target, "Question before closing");
    await vi.waitFor(() =>
      expect(target.textContent).toContain("Partial answer"),
    );

    const dialog = target.closest("dialog");
    dialog.setAttribute("open", "");
    dialog.close();

    await vi.waitFor(() =>
      expect(target.querySelector("input").disabled).toBe(false),
    );
    expect(target.querySelectorAll(".chat-message")).toHaveLength(2);
    expect(target.textContent).toContain("Question before closing");
    expect(target.textContent).toContain("Partial answer");
    expect(target.querySelector('[role="alert"]')).toBeNull();
  });
});
