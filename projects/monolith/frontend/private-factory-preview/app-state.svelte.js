// Minimal `$app/state` for the preview: shallow-routing state only, kept in
// the history entry the way SvelteKit keeps it, so Back restores it.
export const page = $state({ state: {} });

export function pushState(url, state) {
  history.pushState({ "preview:state": state }, "", url || undefined);
  page.state = state;
}

export function replaceState(url, state) {
  history.replaceState({ "preview:state": state }, "", url || undefined);
  page.state = state;
}

if (typeof window !== "undefined") {
  window.addEventListener("popstate", (event) => {
    page.state = event.state?.["preview:state"] ?? {};
  });
}
