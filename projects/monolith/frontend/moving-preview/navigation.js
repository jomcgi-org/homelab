export async function goto(url) {
  const local = new URL(location.href);
  local.pathname = url.pathname;
  local.search = url.search;
  history.pushState({}, "", local);
  dispatchEvent(new PopStateEvent("popstate"));
}
