import { describe, expect, test } from "vitest";
import {
  enterTranscript,
  MOBILE_VIEW_LIST,
  MOBILE_VIEW_TRANSCRIPT,
  returnToList,
  transitionMobileView,
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

  test("transitions only respond to mobile view actions", () => {
    const state = { view: MOBILE_VIEW_LIST };
    expect(transitionMobileView(state, "select-session").view).toBe(
      MOBILE_VIEW_TRANSCRIPT,
    );
    expect(
      transitionMobileView({ view: MOBILE_VIEW_TRANSCRIPT }, "back").view,
    ).toBe(MOBILE_VIEW_LIST);
    expect(transitionMobileView(state, "unknown")).toBe(state);
  });
});
