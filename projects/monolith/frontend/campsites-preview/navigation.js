export function invalidateAll() {
  window.campsitesRefreshes = (window.campsitesRefreshes ?? 0) + 1;
  return Promise.resolve();
}
