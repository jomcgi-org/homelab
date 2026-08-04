import { describe, expect, test } from "vitest";
import { statusClass, statusLabel } from "./status.js";

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
