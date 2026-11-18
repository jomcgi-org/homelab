import { readFileSync } from "node:fs";
import { act } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Root } from "./root";
import { StarsPage } from "./stars-page";
import { makeSummary } from "../../data/stars-summary.mjs";
import type { Snapshot } from "./stars-model";

vi.mock("./stars-map", () => ({
  default: () => <p>Interactive map loaded</p>,
}));
const now = Date.parse("2026-11-17T20:00:00Z");
const index = {
  asOf: "2026-11-17T19:30:00Z",
  generatedAt: "2026-11-17T19:30:00Z",
  files: {
    sites: "/data/stars/v/sites-abcdef123456.json",
    historyMap: "/data/stars/v/historyMap-abcdef123456.json",
    summary: "/data/stars/v/summary-abcdef123456.json",
  },
  // History is older than the forecast; the page dates it by its own file.
  sources: { historyMap: { lastModified: "2026-11-17T10:00:00Z" } },
};
const snapshot: Snapshot = {
  fetched_at: "2026-11-17T18:00:00Z",
  count: 1,
  sites: [
    {
      id: "one",
      name: "Galloway",
      lat: 55,
      lon: -4,
      clear_dark_hours: 2,
      best_hours: [
        { time: "2026-11-17T22:00:00Z", cloud_area_fraction: 4 },
        { time: "2026-11-18T23:00:00Z", cloud_area_fraction: 8 },
      ],
    },
  ],
};
const history = {
  sites: snapshot.sites.map((site, i) => ({
    ...site,
    clear: Array.from({ length: 12 }, (_, m) => (m + 1) * (i + 1)),
    dark: Array(12).fill(100),
  })),
};
const fetcher = vi.fn(async (path: string) => {
  const data =
    path === index.files.summary
      ? makeSummary(snapshot, now, index.generatedAt)
      : path === "/data/stars/latest.json"
        ? index
        : path === index.files.sites
          ? snapshot
          : path === index.files.historyMap
            ? history
            : undefined;
  if (!data) throw new Error(`Unexpected request: ${path}`);
  return new Response(JSON.stringify(data));
});
beforeEach(() => {
  vi.spyOn(Date, "now").mockReturnValue(now);
  vi.stubGlobal("fetch", fetcher);
  fetcher.mockClear();
  window.history.replaceState(null, "", "/stars");
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
const page = () =>
  render(
    <Root>
      <StarsPage />
    </Root>,
  );

const advance = (milliseconds: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(milliseconds);
  });

describe("stars static and interactive page", () => {
  it("keeps a stale publication timestamp when a visitor loads its full forecast", async () => {
    vi.useFakeTimers();
    page();
    await advance(50);
    vi.spyOn(Date, "now").mockReturnValue(now + 60 * 60_000);
    await advance(60 * 60_000);
    expect(screen.getByLabelText("Forecast snapshot").textContent).toContain(
      "Stale site snapshot",
    );
    fireEvent.click(
      screen.getByRole("button", { name: "Load live forecasts" }),
    );
    await advance(50);
    expect(screen.getByRole("button", { name: "Galloway" })).toBeTruthy();
    const summary = screen.getByLabelText("Forecast snapshot").textContent;
    expect(summary).toContain("Stale site snapshot");
    expect(summary).toContain(index.generatedAt);
    expect(summary).not.toContain("Stale —");
  });

  it("refreshes the default summary every 30 minutes, retains it on failure and retries without loading large data", async () => {
    vi.useFakeTimers();
    const { unmount } = page();
    await advance(50);
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual([
      "/data/stars/latest.json",
      index.files.summary,
    ]);
    const updatedIndex = {
      ...index,
      files: {
        ...index.files,
        summary: "/data/stars/v/summary-123456abcdef.json",
      },
    };
    const updatedSummary = makeSummary(
      {
        ...snapshot,
        fetched_at: "2026-11-17T20:00:00Z",
        sites: [{ ...snapshot.sites[0]!, name: "North Coast" }],
      },
      now + 30 * 60_000,
    );
    const nextSummary = () =>
      fetcher
        .mockResolvedValueOnce(new Response(JSON.stringify(updatedIndex)))
        .mockResolvedValueOnce(new Response(JSON.stringify(updatedSummary)));
    nextSummary();
    vi.spyOn(Date, "now").mockReturnValue(now + 30 * 60_000);
    await advance(30 * 60_000);
    expect(screen.getByLabelText("Forecast snapshot").textContent).toContain(
      "North Coast",
    );
    expect(
      screen.getByLabelText("Forecast snapshot").querySelector("time")
        ?.dateTime,
    ).toBe(updatedSummary.asOf);
    fetcher.mockRejectedValueOnce(new Error("offline"));
    vi.spyOn(Date, "now").mockReturnValue(now + 10 * 60 * 60_000);
    await advance(30 * 60_000);
    expect(screen.getByLabelText("Forecast snapshot").textContent).toContain(
      "North Coast",
    );
    expect(screen.getByLabelText("Forecast snapshot").textContent).toContain(
      "Stale",
    );
    expect(screen.getByRole("button", { name: "Retry summary" })).toBeTruthy();
    nextSummary();
    fireEvent.click(screen.getByRole("button", { name: "Retry summary" }));
    await advance(50);
    expect(screen.queryByRole("button", { name: "Retry summary" })).toBeNull();
    expect(
      fetcher.mock.calls.every(
        ([path]) =>
          path === "/data/stars/latest.json" || path.includes("/summary-"),
      ),
    ).toBe(true);
    expect(screen.queryByText("Interactive map loaded")).toBeNull();
    const requests = fetcher.mock.calls.length;
    unmount();
    await advance(30 * 60_000);
    expect(fetcher).toHaveBeenCalledTimes(requests);
  });

  it("restores a historical deep link and never requires a map to inspect sites", async () => {
    window.history.replaceState(null, "", "/stars?mode=historical&month=12");
    page();
    await screen.findByRole("button", { name: "Galloway" });
    expect(
      screen
        .getByRole("button", { name: "December" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
    expect(screen.queryByText("Interactive map loaded")).toBeNull();
  });
  it("dates history by its own file and re-dates it when republished unchanged", async () => {
    vi.useFakeTimers();
    window.history.replaceState(null, "", "/stars?mode=historical&month=12");
    page();
    await advance(50);
    // 10 hours old, so stale, although index.asOf is 30 minutes old.
    expect(screen.getByText(/History as of/).textContent).toContain("Stale");
    const lastModified = "2026-11-17T19:45:00Z";
    fetcher.mockResolvedValueOnce(
      new Response(
        JSON.stringify({ ...index, sources: { historyMap: { lastModified } } }),
      ),
    );
    await advance(30 * 60_000);
    const asOf = screen.getByText(/History as of/);
    expect(asOf.querySelector("time")?.dateTime).toBe(lastModified);
    expect(asOf.textContent).not.toContain("Stale");
    vi.useRealTimers();
  });
  it("keeps existing data when a refresh fails and marks it stale as the clock advances", async () => {
    vi.useFakeTimers();
    page();
    fireEvent.click(
      screen.getByRole("button", { name: "Load live forecasts" }),
    );
    await advance(50);
    expect(screen.getByRole("button", { name: "Galloway" })).toBeTruthy();
    fetcher.mockRejectedValueOnce(new Error("offline"));
    vi.spyOn(Date, "now").mockReturnValue(now + 9 * 60 * 60_000);
    await advance(30 * 60_000);
    expect(screen.getByLabelText("Forecast snapshot").textContent).toContain(
      "Galloway",
    );
    expect(screen.getByLabelText("Forecast snapshot").textContent).toContain(
      "Stale",
    );
    expect(screen.getByText(/Could not refresh/)).toBeTruthy();
    expect(screen.queryByText(/unavailable/i)).toBeNull();
    vi.useRealTimers();
  });
  it("keeps the copied title/description, new canonical and legacy redirects", () => {
    const html = readFileSync("stars.html", "utf8");
    expect(html).toContain(
      "<title>Dark-sky stargazing map, Scotland viewing windows</title>",
    );
    expect(html).toContain(
      "A map of curated Scottish dark-sky sites scored by upcoming viewing windows from the met.no forecast, with a historical clear-dark-hours layer by month.",
    );
    expect(html).toContain('href="https://jomcgi.dev/stars"');
    // The whole file, in order, so that an extra rule fails.
    expect(readFileSync("public/_redirects", "utf8")).toBe(
      "/factory /factory/ 200\n/factory/* /factory/ 200\n/app/llm-leaderboard /llm-leaderboard 301\n/app/llm-leaderboard/* /llm-leaderboard 301\n/app/hikes /hikes 301\n/app/hikes/* /hikes 301\n/app/stars /stars 301\n/app/stars/* /stars 301\n",
    );
  });
});
