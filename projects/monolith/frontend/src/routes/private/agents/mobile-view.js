export const MOBILE_VIEW_LIST = "list";
export const MOBILE_VIEW_TRANSCRIPT = "transcript";
export const MOBILE_MEDIA_QUERY = "(max-width: 760px)";

export function enterTranscript(state) {
  return { ...state, view: MOBILE_VIEW_TRANSCRIPT };
}

export function returnToList(state) {
  return { ...state, view: MOBILE_VIEW_LIST };
}
