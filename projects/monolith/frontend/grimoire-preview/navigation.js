export function goto(url, options = {}) {
  const next = new URL(url, location.href);
  history[options.replaceState ? "replaceState" : "pushState"]({}, "", next);
  window.dispatchEvent(new Event("popstate"));
  return Promise.resolve();
}
export const beforeNavigate = () => {};
export const afterNavigate = () => {};
export const invalidateAll = async () => {};
