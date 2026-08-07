import { describe, expect, test } from "vitest";
import { statusClass, statusLabel, vmState } from "./status.js";

test.each([
  [{ status: "running" }, "running", "running"],
  [{ status: "running", pending_count: 1 }, "working", "working"],
  [{ status: "completed" }, "completed", "completed"],
  [{ status: "warn" }, "warn", "warn"],
  [{ status: "needs_input" }, "needs input", "needs_input"],
  [{ status: "unknown" }, "completed", "completed"],
  [null, "completed", "completed"],
])("maps %j to label %s and class %s", (session, label, className) => {
  expect(statusLabel(session)).toBe(label);
  expect(statusClass(session)).toBe(className);
});

describe("agent status", () => {
  test("exports the status mapping helpers", () => {
    expect(statusClass).toBeTypeOf("function");
    expect(statusLabel).toBeTypeOf("function");
  });
});

describe("vmState", () => {
  const vms = {
    "s-awake": { state: "awake" },
    "s-asleep": { state: "asleep" },
    "s-weird": { state: "destroying" },
  };

  test.each([
    [{ ember_session_id: "s-awake" }, "awake"],
    [{ ember_session_id: "s-asleep" }, "asleep"],
    [{ ember_session_id: "s-weird" }, "off"],
    [{ ember_session_id: "s-unknown" }, "off"],
    [{ ember_session_id: null }, "off"],
    [null, "off"],
  ])("maps %j to %s", (session, expected) => {
    expect(vmState(session, vms)).toBe(expected);
  });

  test("is off when the vm map is missing", () => {
    expect(vmState({ ember_session_id: "s-awake" }, null)).toBe("off");
  });
});
