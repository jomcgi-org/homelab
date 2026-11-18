import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { afterEach, expect, it, vi } from "vitest";
import axe from "axe-core";
import { Root } from "../root";
import type { ReactNode } from "react";
import { HikesPage } from "../hikes-page";
import { HikesPlanner } from "./planner";
import { FIXTURE_NOW, FIXTURE_WALK } from "./fixtures";
import { REFRESH_MS } from "./data";
import { STALE_MS, SNAPSHOT_STALE_MS } from "../../../data/hikes-freshness.mjs";

const mount = (element: ReactNode = <HikesPlanner />) =>
  render(<Root>{element}</Root>);
const visit = (query: string) =>
  window.history.replaceState(null, "", "/hikes" + query);
const button = (name = FIXTURE_WALK.name) =>
  screen.queryByRole("button", { name });
const input = (label: string) =>
  screen.getByLabelText(label) as HTMLInputElement;
const advance = async (age: number, elapsed = age) => {
  vi.mocked(Date.now).mockReturnValue(FIXTURE_NOW + age);
  await act(() => vi.advanceTimersByTimeAsync(elapsed));
};
const near = () => screen.getByLabelText("Near") as HTMLSelectElement;
const chooseNear = (value: string) =>
  fireEvent.change(near(), { target: { value } });
const getLocation = () => {
  const request = vi.fn();
  vi.stubGlobal("navigator", {
    ...navigator,
    geolocation: { getCurrentPosition: request },
  });
  return {
    succeed: (lat: number, lon: number) =>
      request.mock.lastCall![0]({ coords: { latitude: lat, longitude: lon } }),
    fail: () => request.mock.lastCall![1]({ code: 1 }),
  };
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  visit("");
});
const response = (data: unknown) => new Response(JSON.stringify(data));
function mockData(walks = [FIXTURE_WALK]) {
  vi.spyOn(Date, "now").mockReturnValue(FIXTURE_NOW);
  const fetch = vi.fn(async (path: string) =>
    response(
      path.endsWith("latest.json")
        ? {
            asOf: new Date(FIXTURE_NOW).toISOString(),
            generatedAt: new Date(FIXTURE_NOW).toISOString(),
            files: { walks: "/data/hikes/v/walks-012345abcdef.json" },
          }
        : {
            schema_version: 2,
            fetched_at: new Date(FIXTURE_NOW).toISOString(),
            walks,
          },
    ),
  );
  vi.stubGlobal("fetch", fetch);
  return fetch;
}
it("requests no large data before intent and retains a published summary on failure", async () => {
  visit("?ref=shared&walk=invalid");
  const fetch = vi.fn().mockRejectedValue(new Error("offline"));
  vi.stubGlobal("fetch", fetch);
  const published =
    '<p>Data as of <time datetime="2026-06-15T06:00:00Z">2026-06-15T06:00:00Z</time></p><ul><li>Published Ben Vorlich</li></ul>';
  mount(<HikesPage published={published} />);
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Explore walks" }));
  await screen.findByRole("button", { name: "Retry data" });
  expect(screen.getByText("Published Ben Vorlich")).toBeTruthy();
  expect(document.body.textContent).not.toContain("unavailable");
});
it("opens a shared page link without a click, restores its walk and filters, shows its table and passes axe", async () => {
  mockData();
  visit(`?walk=${FIXTURE_WALK.id}&near=loch-lomond&dmax=6`);
  const { container } = mount(<HikesPage />);
  await screen.findByRole("table");
  expect(
    screen
      .getByRole("link", { name: `${FIXTURE_WALK.name} ↗` })
      .getAttribute("href"),
  ).toBe(FIXTURE_WALK.url);
  expect(screen.getByRole("columnheader", { name: "Rain" })).toBeTruthy();
  expect(screen.getByRole("button", { name: "Mon 15 Jun" })).toBeTruthy();
  expect(near().value).toBe("loch-lomond");
  expect(input("Max duration (h)").value).toBe("6");
  expect(container.querySelector("[style]")).toBeNull();
  expect(
    (
      await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      })
    ).violations,
  ).toEqual([]);
  fireEvent.change(input("Max ascent (m)"), { target: { value: "100" } });
  expect(screen.queryByRole("table")).toBeNull();
  expect(screen.getByText(/No walks match/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Reset" }));
  expect(button()).toBeTruthy();
});
it("keeps the last forecast through failed refreshes and rolls away elapsed hours", async () => {
  const fetch = mockData();
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  await act(async () => {
    mount();
  });
  expect(button()).toBeTruthy();
  fetch.mockRejectedValue(new Error("offline"));
  await advance(3 * REFRESH_MS);
  expect(screen.getByText(/Showing the last loaded data/)).toBeTruthy();
  expect(screen.queryByText(/Stale forecast/)).toBeNull();
  // The remaining five hours still fit; after another hour they no longer do.
  expect(button()).toBeTruthy();
  vi.mocked(Date.now).mockReturnValue(FIXTURE_NOW + 2 * 3_600_000);
  await act(async () => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
  expect(button()).toBeNull();
});
it("hydrates the deployment's published HTML without replacing it", async () => {
  const published =
    '<!--hikes-summary:start--><p>Data as of <time datetime="2026-06-15T06:00:00Z">2026-06-15T06:00:00Z</time></p><ul><li>Ben Vorlich</li></ul><!--hikes-summary:end-->';
  const element = (
    <Root>
      <HikesPage published={published} />
    </Root>
  );
  const container = document.createElement("div");
  container.innerHTML = renderToString(element);
  document.body.append(container);
  const errors: unknown[] = [];
  let root: ReturnType<typeof hydrateRoot>;
  await act(async () => {
    root = hydrateRoot(container, element, {
      onRecoverableError: (e) => errors.push(e),
    });
  });
  expect(errors).toEqual([]);
  expect(container.querySelector("#hikes-published")!.innerHTML).toBe(
    published,
  );
  await act(async () => root!.unmount());
  container.remove();
});
it.each(["denied", "missing"])(
  "resets %s geolocation to Anywhere and keeps regions usable",
  async (mode) => {
    mockData();
    const location = mode === "denied" ? getLocation() : null;
    if (!location)
      vi.stubGlobal("navigator", { ...navigator, geolocation: undefined });
    visit("?near=__me__");
    mount();
    if (location) act(() => location.fail());
    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toContain(
        mode === "denied"
          ? "Location permission denied"
          : "Geolocation not available",
      ),
    );
    expect(near().value).toBe("");
    expect(new URLSearchParams(window.location.search).has("near")).toBe(false);
    chooseNear("loch-lomond");
    expect(screen.queryByRole("alert")).toBeNull();
  },
);

it("loads shared filters alone and restores a shared query on browser navigation", async () => {
  mockData();
  visit("?near=loch-lomond&dmax=6");
  mount(<HikesPage />);
  await screen.findByRole("button", { name: FIXTURE_WALK.name });
  expect(input("Max duration (h)").value).toBe("6");
  act(() => {
    visit(`?walk=${FIXTURE_WALK.id}`);
    window.dispatchEvent(new PopStateEvent("popstate"));
  });
  await screen.findByRole("table");
});
it.each(["2026-06-12T06:00:00Z", "2026-06-15T04:00:00Z", null])(
  "shows a retained walk forecast age (%s) even when publication and another forecast are new",
  async (at) => {
    mockData([
      { ...FIXTURE_WALK, forecast_fetched_at: at },
      {
        ...FIXTURE_WALK,
        id: "aaaaaaaa-0000-5000-8000-000000000002",
        name: "Current walk",
      },
    ]);
    visit(`?walk=${FIXTURE_WALK.id}`);
    mount(<HikesPage />);
    await screen.findByRole("table");
    const card = screen.getByRole("complementary", { name: FIXTURE_WALK.name });
    expect(card.textContent).toContain(
      at === null
        ? "Forecast age unknown"
        : at.includes("12T")
          ? "Stale forecast"
          : "Earlier forecast retained",
    );
    if (at)
      expect(card.querySelector("time")?.dateTime).toBe(
        new Date(at).toISOString(),
      );
    expect(document.body.textContent).not.toContain("unavailable");
    expect(screen.getByRole("button", { name: "Current walk" })).toBeTruthy();
  },
);
it("holds a shared location selection until coordinates arrive and then applies the radius", async () => {
  const far = {
    ...FIXTURE_WALK,
    id: "aaaaaaaa-0000-5000-8000-000000000002",
    name: "Distant walk",
    lat: 58.5,
    lon: -3,
  };
  mockData([FIXTURE_WALK, far]);
  const location = getLocation();
  visit("?near=__me__&walk=" + FIXTURE_WALK.id);
  mount();
  await screen.findByRole("button", { name: "Show map" });
  expect(screen.getByText("Finding your location…")).toBeTruthy();
  expect(button()).toBeNull();
  expect(button(far.name)).toBeNull();
  expect(screen.queryByText(/No walks match/)).toBeNull();
  expect(new URLSearchParams(window.location.search).get("walk")).toBe(
    FIXTURE_WALK.id,
  );
  act(() => location.succeed(FIXTURE_WALK.lat, FIXTURE_WALK.lon));
  await screen.findByRole("table");
  expect(button()).toBeTruthy();
  expect(button(far.name)).toBeNull();
});
it("ignores a location callback after the reader changes region", async () => {
  mockData();
  const location = getLocation();
  mount();
  await screen.findByRole("button", { name: FIXTURE_WALK.name });
  chooseNear("__me__");
  expect(button()).toBeNull();
  chooseNear("loch-lomond");
  act(() => {
    location.succeed(0, 0);
    location.fail();
  });
  expect(near().value).toBe("loch-lomond");
  expect(button()).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
});
it("marks a missed site refresh stale after 90 minutes without marking normal weather stale", async () => {
  mockData();
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  await act(async () => {
    mount();
  });
  await advance(SNAPSHOT_STALE_MS);
  expect(screen.getByText(/Stale site snapshot/)).toBeTruthy();
  expect(screen.queryByText(/Stale forecast/)).toBeNull();
  expect(button()).toBeTruthy();
  expect(document.body.textContent).not.toContain("unavailable");
});
it.each([
  [
    STALE_MS,
    "forecast",
    '<span data-forecast-freshness data-fetched-at="2026-06-15T06:00:00Z" data-latest-fetched-at="2026-06-15T06:00:00Z">Forecast refreshes every 2 hours</span>',
  ],
  [
    SNAPSHOT_STALE_MS,
    "site snapshot",
    '<span data-snapshot-freshness data-generated-at="2026-06-15T06:00:00Z">Site refreshes every 30 minutes</span>',
  ],
] as const)(
  "ages published data after %i ms (%s) before planner intent",
  async (age, kind, status) => {
    vi.spyOn(Date, "now").mockReturnValue(FIXTURE_NOW + age - 1);
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    mount(
      <HikesPage
        published={
          status +
          '<p>Data as of <time datetime="2026-06-15T12:00:00Z">recent upload</time></p><ul><li>Published Ben Vorlich</li></ul>'
        }
      />,
    );
    const stale = new RegExp("Stale " + kind);
    expect(screen.queryByText(stale)).toBeNull();
    await advance(age, 60_000);
    expect(screen.getByText(stale)).toBeTruthy();
    expect(screen.getByText("Published Ben Vorlich")).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  },
);
