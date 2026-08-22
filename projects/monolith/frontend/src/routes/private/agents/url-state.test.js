import { describe, expect, test } from "vitest";
import {
  backToRun,
  clearSelection,
  parseUrlState,
  selectRun,
  selectSession,
  setVoiceMode,
  withSearch,
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
      mode: null,
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

  test("keeps the path the browser is on rather than the route id", () => {
    // private.jomcgi.dev serves this page at /agents; /private/agents is only
    // the internal route src/hooks.js reroutes onto. Navigating to the route id
    // rewrote the address bar to a URL nobody types.
    expect(withSearch("/agents", "run=wf-123")).toBe("/agents?run=wf-123");
  });

  test("drops the question mark when nothing is selected", () => {
    expect(withSearch("/agents", "")).toBe("/agents");
  });

  test("composes with the transitions it exists to serve", () => {
    const search = selectSession(selectRun("", "wf-1"), "wf-1-call2");
    expect(withSearch("/agents", search)).toBe(
      "/agents?run=wf-1&session=wf-1-call2",
    );
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
      mode: null,
    });
  });

  test("voice mode round-trips and leaving preserves selection", () => {
    const entered = setVoiceMode("run=wf-123", true);
    expect(parseUrlState(entered)).toEqual({
      runId: "wf-123",
      sessionId: null,
      mode: "voice",
    });
    expect(setVoiceMode(entered, false)).toBe("run=wf-123");
  });

  test("ignores unknown modes", () => {
    expect(parseUrlState("mode=quiet").mode).toBeNull();
  });
});
