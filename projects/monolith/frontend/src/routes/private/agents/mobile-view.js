export const MOBILE_VIEW_LIST = "list";
export const MOBILE_VIEW_TRANSCRIPT = "transcript";

export function enterTranscript(state) {
  return { ...state, view: MOBILE_VIEW_TRANSCRIPT };
}

export function returnToList(state) {
  return { ...state, view: MOBILE_VIEW_LIST };
}

export function transitionMobileView(state, action) {
  if (action === "select-session") return enterTranscript(state);
  if (action === "back") return returnToList(state);
  return state;
}
