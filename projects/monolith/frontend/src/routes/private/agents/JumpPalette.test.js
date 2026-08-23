// @vitest-environment happy-dom
import { afterEach, describe, expect, test, vi } from "vitest";
import { mount, tick, unmount } from "svelte";
import JumpPalette from "./JumpPalette.svelte";
import { RUN_LEXICON as P } from "./run-lexicon.js";

const mounted = [];

function session(id, title, status = "completed") {
  return {
    id,
    title,
    status,
    created_at: "2026-08-22T10:00:00Z",
    last_turn_at: "2026-08-22T11:00:00Z",
  };
}

function run(id, title) {
  return {
    workflow_id: id,
    title,
    state: "completed",
    completed_at: "2026-08-22T11:00:00Z",
  };
}

function callbacks(overrides = {}) {
  return {
    onClose: vi.fn(),
    onOpenRun: vi.fn(),
    onOpenSession: vi.fn(),
    onNewSession: vi.fn(),
    onSearchTurns: vi.fn(),
    ...overrides,
  };
}

async function render(props = {}) {
  const target = document.createElement("div");
  document.body.append(target);
  const component = mount(JumpPalette, {
    target,
    props: {
      open: true,
      query: "",
      sessions: [],
      runs: [],
      terminalRuns: [],
      inbox: { needsYou: [], running: [] },
      ...callbacks(),
      ...props,
    },
  });
  mounted.push({ component, target });
  await tick();
  return target;
}

async function press(target, key, init = {}) {
  target.dispatchEvent(
    new KeyboardEvent("keydown", { key, bubbles: true, ...init }),
  );
  await tick();
}

afterEach(async () => {
  vi.restoreAllMocks();
  for (const { component, target } of mounted.splice(0)) {
    await unmount(component);
    target.remove();
  }
});

describe("jump palette keyboard behavior", () => {
  test("uses the human decision kind as a needs-you run's ask word", async () => {
    const gated = {
      workflow_id: "wf-1",
      title: "Push a branch",
      state: "blocked",
      updated_at: "2026-08-22T11:00:00Z",
      needs: { kind: "human", decision_kind: "push_gate" },
      shape: [
        {
          key: "push_gate",
          kind: "gate",
          state: "blocked",
        },
      ],
    };
    const target = await render({
      runs: [gated],
      inbox: {
        needsYou: [
          {
            kind: "run",
            id: "wf-1",
            value: gated,
            activityAt: gated.updated_at,
          },
        ],
        running: [],
      },
    });

    expect(target.textContent).toContain("Approve push");
  });

  test("opens voice companion from Actions", async () => {
    const onOpenVoice = vi.fn();
    const target = await render({ onOpenVoice });
    const voice = [...target.querySelectorAll('[role="option"]')].find(
      (option) => option.textContent.includes(P.labels.openVoiceCompanion),
    );

    voice.click();
    await tick();

    expect(onOpenVoice).toHaveBeenCalledTimes(1);
  });

  test("renders an accessible dialog and focuses its combobox", async () => {
    const target = await render();
    const dialog = target.querySelector('[role="dialog"]');
    const input = target.querySelector('[role="combobox"]');

    expect(dialog?.getAttribute("aria-modal")).toBe("true");
    await vi.waitFor(() => expect(document.activeElement).toBe(input));
  });

  test("wraps between the first inbox row and last action row", async () => {
    const active = session("session-1", "Inbox session", "needs_input");
    const target = await render({
      query: "session",
      sessions: [active],
      inbox: {
        needsYou: [
          {
            kind: "session",
            id: active.id,
            value: active,
            activityAt: active.last_turn_at,
          },
        ],
        running: [],
      },
    });
    const input = target.querySelector('[role="combobox"]');
    const options = () => [...target.querySelectorAll('[role="option"]')];

    await press(input, "ArrowUp");
    expect(options().at(-1)?.getAttribute("aria-selected")).toBe("true");

    await press(input, "ArrowDown");
    expect(options()[0]?.getAttribute("aria-selected")).toBe("true");
  });

  test("closes on Escape", async () => {
    const onClose = vi.fn();
    const target = await render({ onClose });

    await press(target.querySelector('[role="combobox"]'), "Escape");

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("opens the highlighted session or run", async () => {
    const selectedSession = session("session-7", "Selected session");
    const onOpenSession = vi.fn();
    const sessionTarget = await render({
      sessions: [selectedSession],
      onOpenSession,
    });
    await press(sessionTarget.querySelector('[role="combobox"]'), "Enter");
    expect(onOpenSession).toHaveBeenCalledWith("session-7");

    const onOpenRun = vi.fn();
    const runTarget = await render({
      terminalRuns: [run("run-9", "Selected run")],
      onOpenRun,
    });
    await press(runTarget.querySelector('[role="combobox"]'), "Enter");
    expect(onOpenRun).toHaveBeenCalledWith("run-9");
  });

  test("searches turns with Shift Enter regardless of highlight", async () => {
    const onSearchTurns = vi.fn();
    const target = await render({
      query: "find this turn",
      sessions: [session("session-1", "Find this turn")],
      onSearchTurns,
    });
    const input = target.querySelector('[role="combobox"]');
    await press(input, "ArrowDown");
    await press(input, "Enter", { shiftKey: true });

    expect(onSearchTurns).toHaveBeenCalledWith("find this turn");
  });

  test("opens rows in a new tab but not actions", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const selectedSession = session("session-3", "Open elsewhere");
    const rowTarget = await render({ sessions: [selectedSession] });
    await press(rowTarget.querySelector('[role="combobox"]'), "Enter", {
      metaKey: true,
    });

    expect(open).toHaveBeenCalledTimes(1);
    expect(open.mock.calls[0][0]).toContain("session=session-3");

    const actionTarget = await render();
    await press(actionTarget.querySelector('[role="combobox"]'), "Enter", {
      metaKey: true,
    });
    expect(open).toHaveBeenCalledTimes(1);
  });

  test("resets the highlight when the query changes", async () => {
    const target = await render({
      sessions: [
        session("session-1", "First session"),
        session("session-2", "Second session"),
      ],
    });
    const input = target.querySelector('[role="combobox"]');
    await press(input, "ArrowDown");
    expect(
      target
        .querySelectorAll('[role="option"]')[1]
        ?.getAttribute("aria-selected"),
    ).toBe("true");

    input.value = "session";
    input.dispatchEvent(new Event("input", { bubbles: true }));
    await tick();

    expect(
      target
        .querySelectorAll('[role="option"]')[0]
        ?.getAttribute("aria-selected"),
    ).toBe("true");
  });

  test("describes the loaded history scope for an empty result", async () => {
    const target = await render({
      query: "missing",
      sessions: [
        session("session-1", "First session"),
        session("session-2", "Second session"),
      ],
    });

    expect(target.textContent).toContain(P.labels.jumpNoMatches);
    expect(target.textContent).toContain(
      P.labels.jumpHistoryScope.replace("{count}", "2"),
    );
  });

  test("keeps Tab on the input instead of focusing an option", async () => {
    const target = await render({
      sessions: [session("session-1", "Only session")],
    });
    const input = target.querySelector('[role="combobox"]');
    input.focus();

    await press(input, "Tab");

    expect(document.activeElement).toBe(input);
    expect(
      [...target.querySelectorAll('[role="option"]')].every(
        (option) => option.getAttribute("tabindex") === "-1",
      ),
    ).toBe(true);
  });
});
