import { describe, expect, test } from "vitest";
import {
  backToRun,
  clearSelection,
  parseUrlState,
  selectRun,
  selectSession,
} from "./url-state.js";

describe("agent URL state", () => {
  test("parses URL and returns null for absent params", () => {
    expect(
      parseUrlState(
        new URL("/private/agents?run=wf-123", "https://example.test"),
      ),
    ).toEqual({
      runId: "wf-123",
      sessionId: null,
    });
  });

  test("selecting a session from a run preserves the run", () => {
    expect(
      selectSession(new URLSearchParams("run=wf-123"), "wf-123-call2"),
    ).toBe("run=wf-123&session=wf-123-call2");
  });

  test("selecting a run drops the session", () => {
    expect(selectRun("run=old&session=old-call1", "wf-456")).toBe("run=wf-456");
  });

  test("clearing selection yields a bare query string", () => {
    expect(clearSelection("run=wf-123&session=wf-123-call2")).toBe("");
  });

  test("back to run drops only the session", () => {
    expect(backToRun("run=wf-123&session=wf-123-call2")).toBe("run=wf-123");
  });

  test("encodes ids and round-trips them", () => {
    const sessionId = "wf-parent-call#2/{child}";
    const search = selectSession("run=wf-parent", sessionId);
    expect(
      parseUrlState(
        new URL(`/private/agents?${search}`, "https://example.test"),
      ),
    ).toEqual({
      runId: "wf-parent",
      sessionId,
    });
  });
});
