import { describe, expect, test } from "vitest";
import {
  HOTKEYS,
  chatBody,
  decisionBody,
  effectLine,
  moveCursor,
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
