export function replaceState(url, state) {
  const target = new URL(url, location.href);
  if (
    target.origin !== location.origin ||
    target.pathname !== location.pathname
  ) {
    throw new Error("Fixture navigation must stay on this page");
  }
  history.replaceState(state, "", target);
}
