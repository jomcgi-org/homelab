export function replaceState(url, state) {
  const target = new URL(url, location.href);
  if (
    target.origin !== location.origin ||
    target.pathname !== location.pathname
  ) {
    throw new Error("Fixture navigation must stay on this page");
  }
  // Svelte's page.state is a Proxy, which native History cannot clone.
  // The bounded fixture router supports JSON state only.
  history.replaceState(JSON.parse(JSON.stringify(state)), "", target);
}
