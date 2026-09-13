import { describe, expect, test } from "vitest";
import {
  ESCAPE_HOTKEYS,
  HOTKEYS,
  chatBody,
  confirmLine,
  decisionBody,
  effectLine,
  escapeForKey,
  escapeHotkey,
  escapesFor,
  moveCursor,
  needsConfirm,
  open,
  optionForKey,
  resolutionLine,
  resolved,
} from "./escalations-view.js";

function item(overrides = {}) {
  return {
    receipt_id: 3,
    issue_number: 6002,
    title: "escalations are decisions",
    open: true,
    options: [
      {
        key: "split",
        label: "Split the console out",
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
    ...overrides,
  };
}

describe("escalations view helpers", () => {
  test("splits the list into what is waiting and what was decided", () => {
    const list = [item(), item({ receipt_id: 4, open: false })];
    expect(open(list).map((e) => e.receipt_id)).toEqual([3]);
    expect(resolved(list).map((e) => e.receipt_id)).toEqual([4]);
    expect(open(undefined)).toEqual([]);
  });

  test("a resolution reads as the label and who chose it", () => {
    const decided = item({
      open: false,
      resolved: {
        option_key: "close",
        label: "Close as superseded",
        actor: "joe",
      },
    });
    expect(resolutionLine(decided)).toBe("Close as superseded by joe");
    expect(resolutionLine(item())).toBe("");
  });

  test("a split says how many issues it opens", () => {
    expect(effectLine(item().options[0])).toBe(
      "opens 2 issues, closes this one",
    );
    expect(effectLine({ effect: "split", children: 1 })).toBe(
      "opens 1 issue, closes this one",
    );
    expect(effectLine({ effect: "hold" })).toContain("exactly as it is");
    expect(effectLine(null)).toBe("");
    expect(
      effectLine({
        effect: "supersede",
        closes: [7, 8],
        in_favour_of: 10,
      }),
    ).toBe("Closes #7, #8 as superseded by #10");
    // A delivery card's buttons end a task and abandon its branch, which the
    // identical button on a refine card does not.
    expect(effectLine({ effect: "agent-ready" }, "delivery")).toContain(
      "re-admits the work",
    );
    expect(effectLine({ effect: "close" }, "delivery")).toContain(
      "abandons the branch",
    );
    expect(effectLine({ effect: "hold" }, "delivery")).toContain(
      "cancels the task",
    );
    expect(effectLine({ effect: "escape-close" }, "delivery")).toContain(
      "cancels the task",
    );
    expect(effectLine({ effect: "escape-defer" }, "delivery")).toContain(
      "cancels the task",
    );
    expect(effectLine({ effect: "escape-dismiss" }, "delivery")).toContain(
      "stays escalated",
    );
    expect(effectLine({ effect: "split", children: 2 }, "delivery")).toBe(
      "opens 2 issues, closes this one, cancels the task",
    );
  });

  test("the cursor is bounded rather than wrapping", () => {
    expect(moveCursor(0, 1, 3)).toBe(1);
    expect(moveCursor(2, 1, 3)).toBe(2);
    expect(moveCursor(0, -1, 3)).toBe(0);
    expect(moveCursor(0, 1, 0)).toBe(-1);
  });

  test("number keys pick positionally, so 1 is always the recommendation", () => {
    expect(optionForKey(item(), "1").key).toBe("split");
    expect(optionForKey(item(), "2").key).toBe("hold");
    expect(optionForKey(item(), "3")).toBeNull();
    expect(optionForKey(item(), "x")).toBeNull();
    expect(HOTKEYS).toEqual(["1", "2", "3", "4"]);
  });

  test("the escape keys never collide with the brief's numbered ones", () => {
    expect(Object.values(ESCAPE_HOTKEYS)).toEqual(["x", "d", "Escape"]);
    expect(
      HOTKEYS.some((key) => Object.values(ESCAPE_HOTKEYS).includes(key)),
    ).toBe(false);
    expect(escapeForKey(item(), "x").key).toBe("escape:close");
    expect(escapeForKey(item(), "d").key).toBe("escape:defer");
    expect(escapeForKey(item(), "Escape").key).toBe("escape:dismiss");
    expect(escapeForKey(item(), "1")).toBeNull();
    expect(optionForKey(item(), "x")).toBeNull();
  });

  test("a resolved card offers no way out, because there is nothing to leave", () => {
    expect(escapesFor(item({ escape: [] }))).toEqual([]);
    expect(escapesFor(undefined)).toEqual([]);
    expect(escapeForKey(item({ escape: [] }), "x")).toBeNull();
  });

  test("Escape reads as Esc on the button, the other two as themselves", () => {
    const [close, defer, dismiss] = item().escape;
    expect(escapeHotkey(close)).toBe("x");
    expect(escapeHotkey(defer)).toBe("d");
    expect(escapeHotkey(dismiss)).toBe("Esc");
    expect(escapeHotkey({ key: "split" })).toBe("");
  });

  test("only the close is confirmed, and the line says what it will do", () => {
    const [close, defer, dismiss] = item().escape;
    expect(needsConfirm(close)).toBe(true);
    expect(needsConfirm(defer)).toBe(false);
    expect(needsConfirm(dismiss)).toBe(false);
    expect(confirmLine(item())).toContain("Close #6002 as not planned");
    expect(confirmLine(item())).toContain("drop needs-human");
  });

  test("each escape says what it does in the line under its label", () => {
    const [close, defer, dismiss] = item().escape;
    expect(effectLine(close)).toContain("not planned");
    expect(effectLine(defer)).toContain("needs-thought");
    expect(effectLine(dismiss)).toContain("keeps needs-human");
  });

  test("an empty note is left out of the body rather than sent blank", () => {
    expect(decisionBody("close", "  ")).toEqual({ option_key: "close" });
    expect(decisionBody("close", " agreed ")).toEqual({
      option_key: "close",
      note: "agreed",
    });
    expect(chatBody(" more please ")).toEqual({
      action: "chat",
      note: "more please",
    });
  });
});
