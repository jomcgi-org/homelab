import fixtures from "./.generated/pages.json";
const input = new URL(location.href).searchParams;
const views = new Set(["overview", "activity", "context", "chapter", "search"]);
const scenarios = new Set(["live", "empty", "error"]);
export const fixture = $state({
  view: views.has(input.get("view")) ? input.get("view") : "overview",
  scenario: scenarios.has(input.get("scenario"))
    ? input.get("scenario")
    : "live",
});
export const page = $state({
  params: {},
  data: {},
  state: {},
  url: new URL("https://fixture.invalid/slop/factory"),
});

export function syncPage() {
  const route =
    fixture.view === "overview"
      ? ""
      : fixture.view === "activity"
        ? "/activity"
        : "/context";
  page.url = new URL(`/slop/factory${route}`, "https://fixture.invalid");
  if (fixture.view === "chapter")
    page.url.searchParams.set("entity", "synthetic-project");
  if (fixture.view === "search") page.url.searchParams.set("q", "Synthetic");
  page.data = fixtures[`${fixture.scenario}/${fixture.view}`];
}
syncPage();

export function navigate(url) {
  const target = new URL(url, location.href);
  if (target.pathname === "/slop/factory") fixture.view = "overview";
  else if (target.pathname === "/slop/factory/activity")
    fixture.view = "activity";
  else if (target.pathname === "/slop/factory/context") {
    fixture.view = target.searchParams.has("entity")
      ? "chapter"
      : target.searchParams.has("q")
        ? "search"
        : "context";
  } else return false;
  const physical = new URL(location.href);
  physical.search = new URLSearchParams({
    view: fixture.view,
    scenario: fixture.scenario,
  });
  history.pushState({}, "", physical);
  syncPage();
  return true;
}

addEventListener("popstate", () => {
  const params = new URL(location.href).searchParams;
  fixture.view = views.has(params.get("view"))
    ? params.get("view")
    : "overview";
  syncPage();
});
