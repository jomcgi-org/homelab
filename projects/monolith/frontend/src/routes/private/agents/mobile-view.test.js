import { describe, expect, test } from "vitest";
import {
  enterTranscript,
  MOBILE_MEDIA_QUERY,
  MOBILE_VIEW_LIST,
  MOBILE_VIEW_TRANSCRIPT,
  returnToList,
} from "./mobile-view.js";

describe("mobile agent views", () => {
  test("entering transcript preserves unrelated state", () => {
    const state = {
      view: MOBILE_VIEW_LIST,
      selectedId: 42,
      detail: { ok: true },
    };
    expect(enterTranscript(state)).toEqual({
      ...state,
      view: MOBILE_VIEW_TRANSCRIPT,
    });
  });

  test("returning to the list preserves the selected session and detail", () => {
    const state = {
      view: MOBILE_VIEW_TRANSCRIPT,
      selectedId: 42,
      detail: { ok: true },
    };
    expect(returnToList(state)).toEqual({ ...state, view: MOBILE_VIEW_LIST });
  });

  test("uses the shared mobile media query", () => {
    expect(MOBILE_MEDIA_QUERY).toBe("(max-width: 760px)");
  });
});
