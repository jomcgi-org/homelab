import { act } from "react";
import { hydrateRoot } from "react-dom/client";
import { renderToString } from "react-dom/server";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Root } from "./root";
import { TripsPage } from "./trips-page";
import { TripPage } from "./trip-page";
import { TripDayPage } from "./trip-day-page";
import { TripTimelinePage } from "./trip-timeline-page";
import { tripEntries } from "./trips-render";
import { isStale, photoUrl } from "./trips-data";
import { LazyMap, PhotoGrid } from "./trips-ui";
import type { Latest, Payload } from "./trips-types";

const payload: Payload = {
  trip: {
    slug: "2025-trip",
    title: "A trip & journey",
    short_title: "Journey",
    subtitle: "Along the road",
    tz: "America/Vancouver",
    days: { "1": { label: "To the mountains" } },
    highlights: [],
    stats: {},
  },
  points: Array.from({ length: 15 }, (_, i) => ({
    id: String(i),
    lat: 58 + i / 100,
    lng: -125,
    taken_at: `2025-07-${i === 14 ? "02" : "01"}T12:${String(i).padStart(2, "0")}:00Z`,
    image: "img_aaaaaaaaaaaa.jpg",
    tags: [i % 2 ? "Road" : "Mountain"],
    elevation: i * 10,
  })),
};
const latest: Latest = {
  asOf: "2026-11-17T00:00:00Z",
  refreshSeconds: 86400,
  files: { "2025-trip": "/data/trips/v/2025-trip-abcdef123456.json" },
  photoBase: "/data/trips/photos",
  photoCount: 1,
  photoAssetCount: 5,
  photoMode: "snapshot",
  generatedAt: "2026-11-20T00:00:00Z",
};
const entries = tripEntries(latest, { trips: [payload.trip] }, [payload]);
const trip = entries.find((e) => e.template === "trip")!.boot;
const timeline = entries.find((e) => e.template === "trip-timeline")!.boot;
const day = entries.find((e) => e.template === "trip-day")!.boot;
const fetchMock = () =>
  vi.fn(
    async (url: string) =>
      new Response(
        JSON.stringify(url.endsWith("latest.json") ? latest : payload),
      ),
  );
afterEach(() => vi.unstubAllGlobals());

describe("populated static trip pages", () => {
  for (const [Page, boot] of [
    [TripsPage, entries[0]!.boot],
    [TripPage, trip],
    [TripTimelinePage, timeline],
    [TripDayPage, day],
  ] as const) {
    it(`${Page.name} reads before JS, hydrates, and has no axe violations`, async () => {
      vi.stubGlobal("fetch", fetchMock());
      const node = document.createElement("div");
      document.body.append(node);
      node.innerHTML = renderToString(
        <Root>
          <Page initial={boot} />
        </Root>,
      );
      expect(node.querySelector("h1")?.textContent).toBeTruthy();
      expect(node.textContent).toContain("Data as of");
      expect(node.textContent).toContain("Stale");
      expect(node.querySelector("canvas")).toBeNull();
      const errors: unknown[] = [];
      let root: ReturnType<typeof hydrateRoot>;
      await act(async () => {
        root = hydrateRoot(
          node,
          <Root>
            <Page initial={boot} />
          </Root>,
          { onRecoverableError: (e) => errors.push(e) },
        );
      });
      expect(errors).toEqual([]);
      expect(
        (
          await axe.run(node, {
            rules: { "color-contrast": { enabled: false } },
          })
        ).violations.map((v) => v.id),
      ).toEqual([]);
      await act(async () => root.unmount());
      node.remove();
    });
  }
  it("keeps full points out of the initial hydration data", () => {
    expect(day.photos).toHaveLength(12);
    expect(day.photoCount).toBe(14);
    expect(JSON.stringify(trip.days)).not.toContain('"points"');
    expect(entries.map((e) => e.path)).toContain("/trips/2025-trip/day/2");
  });
  it("marks data stale at three daily refresh intervals", () => {
    const asOf = latest.asOf!;
    expect(isStale(asOf, 86400, Date.parse(asOf) + 3 * 86400000 - 1)).toBe(
      false,
    );
    expect(isStale(asOf, 86400, Date.parse(asOf) + 3 * 86400000)).toBe(true);
  });
  it("loads the content-named data on gallery intent and filters tags", async () => {
    const fetch = fetchMock();
    vi.stubGlobal("fetch", fetch);
    render(
      <Root>
        <TripTimelinePage initial={timeline} />
      </Root>,
    );
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Load all 15 photos" }));
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: /Open photo/ }),
      ).toHaveLength(15),
    );
    expect(fetch).toHaveBeenLastCalledWith(latest.files["2025-trip"]);
    fireEvent.click(screen.getByLabelText("mountain"));
    expect(screen.getAllByRole("button", { name: /Open photo/ })).toHaveLength(
      8,
    );
  });
  it("retains the existing trip after a refresh fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("offline");
      }),
    );
    render(
      <Root>
        <TripPage initial={trip} />
      </Root>,
    );
    await screen.findByText("Refresh failed. Showing the saved trip data.");
    expect(screen.getByText("To the mountains")).toBeTruthy();
    expect(screen.queryByText(/unavailable/i)).toBeNull();
  });
  it("does not request map data until the map button is used", async () => {
    const load = vi.fn(async () => []);
    render(<LazyMap slug="2025-trip" load={load} />);
    expect(load).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Load interactive map" }),
    ).toBeTruthy();
  });
  it("lightbox supports next/previous, Escape, and focus return", async () => {
    // happy-dom lacks the dialog top-layer methods; native dialog owns focus
    // trapping in browsers, while this test covers the component's controls.
    vi.spyOn(HTMLDialogElement.prototype, "showModal").mockImplementation(
      function (this: HTMLDialogElement) {
        this.open = true;
      },
    );
    vi.spyOn(HTMLDialogElement.prototype, "close").mockImplementation(function (
      this: HTMLDialogElement,
    ) {
      this.open = false;
    });
    render(
      <PhotoGrid photos={payload.points.slice(0, 2)} base={latest.photoBase} />,
    );
    const opener = screen.getByRole("button", { name: "Open photo 1" });
    fireEvent.click(opener);
    const dialog = screen.getByRole("dialog");
    fireEvent.keyDown(dialog, { key: "ArrowRight" });
    expect(dialog.textContent).toContain("2 / 2");
    fireEvent.keyDown(dialog, { key: "ArrowLeft" });
    expect(dialog.textContent).toContain("1 / 2");
    fireEvent(dialog, new Event("cancel", { cancelable: true }));
    expect(document.activeElement).toBe(opener);
  });
  it("constructs only preset WebP URLs, never originals", () => {
    expect(photoUrl(latest.photoBase, "img_aaaaaaaaaaaa.jpg", "full")).toBe(
      "/data/trips/photos/img_aaaaaaaaaaaa.jpg/full.webp",
    );
  });
});
