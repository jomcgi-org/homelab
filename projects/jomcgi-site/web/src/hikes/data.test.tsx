import { afterEach, expect, it, vi } from "vitest";
import { isStale, loadWalks, parseWalks } from "./data";
import {
  forecastStatus,
  oldestForecast,
  STALE_MS,
  SNAPSHOT_STALE_MS,
  snapshotStatus,
} from "../../../data/hikes-freshness.mjs";
import {
  DEFAULT_FILTERS,
  distance,
  effort,
  effortCeilings,
  filterWalks,
  readFilters,
  writeFilters,
} from "./filters";
import { FIXTURE_NOW, FIXTURE_WALK } from "./fixtures";

const response = (data: unknown) => new Response(JSON.stringify(data));
afterEach(() => vi.unstubAllGlobals());
it("reads the same-origin index then its content-named data; drops unknown prose", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      response({
        asOf: new Date(FIXTURE_NOW).toISOString(),
        generatedAt: new Date(FIXTURE_NOW + 30 * 60_000).toISOString(),
        files: { walks: "/data/hikes/v/walks-012345abcdef.json" },
      }),
    )
    .mockResolvedValueOnce(
      response({
        schema_version: 2,
        fetched_at: "2026-06-15T06:00:00Z",
        walks: [{ ...FIXTURE_WALK, summary: "Do not render this" }],
      }),
    );
  vi.stubGlobal("fetch", fetch);
  const result = await loadWalks(new AbortController().signal);
  expect(fetch.mock.calls.map(([path]) => path)).toEqual([
    "/data/hikes/latest.json",
    "/data/hikes/v/walks-012345abcdef.json",
  ]);
  expect(result.fetchedAt).toBe("2026-06-15T06:00:00.000Z");
  expect(result.generatedAt).toBe("2026-06-15T06:30:00.000Z");
  expect(result.walks).toEqual([FIXTURE_WALK]);
  expect(result.walks[0]).not.toHaveProperty("summary");
});
it("rejects external or malformed data URLs before a second request", async () => {
  const fetch = vi.fn().mockResolvedValue(
    response({
      asOf: null,
      files: { walks: "https://other.example/walks.json" },
    }),
  );
  vi.stubGlobal("fetch", fetch);
  await expect(loadWalks(new AbortController().signal)).rejects.toThrow(
    "Invalid walks path",
  );
  expect(fetch).toHaveBeenCalledTimes(1);
});
it("rejects unsupported schemas and omits unpublishable walks and invalid hours", () => {
  expect(() => parseWalks({ schema_version: 1, walks: [] })).toThrow();
  expect(
    parseWalks({
      schema_version: 2,
      walks: [
        { ...FIXTURE_WALK, duration_h: 0 },
        { ...FIXTURE_WALK, url: "javascript:alert(1)" },
      ],
    }),
  ).toEqual([]);
  expect(
    parseWalks({
      schema_version: 2,
      walks: [{ ...FIXTURE_WALK, hours: [[1, null, 0, 0, 0, null, null]] }],
    })[0]!.hours,
  ).toEqual([]);
});
it("allows normal forecast cycles and site delay; goes stale at three cycles plus delay", () => {
  const fetchedAt = new Date(FIXTURE_NOW).toISOString();
  // Forecast runs every 2h, site snapshots every 30min, not every 90min.
  expect(isStale(fetchedAt, FIXTURE_NOW + 2.5 * 3_600_000)).toBe(false);
  expect(STALE_MS).toBe(6.5 * 3_600_000);
  expect(isStale(fetchedAt, FIXTURE_NOW + STALE_MS - 1)).toBe(false);
  expect(isStale(fetchedAt, FIXTURE_NOW + STALE_MS)).toBe(true);
});
it("ages site snapshots at three refresh intervals independently of forecast age", () => {
  const generatedAt = new Date(FIXTURE_NOW).toISOString();
  expect(SNAPSHOT_STALE_MS).toBe(90 * 60_000);
  expect(snapshotStatus(generatedAt, FIXTURE_NOW + SNAPSHOT_STALE_MS - 1)).toBe(
    "Site refreshes every 30 minutes",
  );
  expect(snapshotStatus(generatedAt, FIXTURE_NOW + SNAPSHOT_STALE_MS)).toBe(
    "Stale site snapshot · published data retained",
  );
  for (const at of [null, "bad"])
    expect(snapshotStatus(at, FIXTURE_NOW)).toBe(
      "Snapshot age unknown · published data retained",
    );
});
it("keeps each walk fetch time, including old, missing and invalid timestamps", () => {
  const latest = "2026-06-15T06:00:00Z";
  const retained = "2026-06-15T04:00:00Z";
  const old = "2026-06-12T06:00:00Z";
  const walks = parseWalks({
    schema_version: 2,
    walks: [old, retained, null, "bad"].map((at) => ({
      ...FIXTURE_WALK,
      forecast_fetched_at: at,
    })),
  });
  expect(walks.map((w) => w.forecast_fetched_at)).toEqual([
    "2026-06-12T06:00:00.000Z",
    "2026-06-15T04:00:00.000Z",
    null,
    null,
  ]);
  expect(oldestForecast(walks.map((w) => w.forecast_fetched_at))).toBe(
    "2026-06-12T06:00:00.000Z",
  );
  expect(forecastStatus(old, latest, FIXTURE_NOW)).toContain("Stale forecast");
  expect(forecastStatus(retained, latest, FIXTURE_NOW)).toContain(
    "Earlier forecast retained",
  );
  expect(forecastStatus(null, latest, FIXTURE_NOW)).toContain(
    "Forecast age unknown",
  );
});
it("keeps the hub's shared URL keys and ignores invalid bounds and locations", () => {
  const state = {
    ...DEFAULT_FILTERS,
    dmin: "1.5",
    dmax: "8",
    kmax: "20",
    ascent: "1000",
    day: "2026-06-15",
    near: "loch-lomond",
    walk: FIXTURE_WALK.id,
  };
  const params = new URLSearchParams("ref=shared");
  writeFilters(params, state);
  expect(readFilters(params)).toEqual(state);
  expect(params.get("ref")).toBe("shared");
  expect(
    readFilters(
      new URLSearchParams("dmin=-1&dmax=Infinity&near=nope&walk=nope&day=nope"),
    ),
  ).toEqual(DEFAULT_FILTERS);
});
it("uses inclusive limits, day membership, nearest-first radius and stable effort", () => {
  const walk = { ...FIXTURE_WALK, viable_days: ["2026-06-15"] };
  const filters = {
    ...DEFAULT_FILTERS,
    dmin: "5.5",
    dmax: "5.5",
    kmin: "11",
    kmax: "11",
    ascent: "920",
  };
  expect(filterWalks([walk], filters, walk)).toEqual([walk]);
  expect(filterWalks([walk], { ...filters, day: "2026-06-16" }, null)).toEqual(
    [],
  );
  expect(filterWalks([walk], filters, { lat: 51.5, lon: 0 })).toEqual([]);
  expect(
    filterWalks([{ ...walk, viable_days: [] }], DEFAULT_FILTERS, null),
  ).toEqual([]);
  expect(distance(walk, walk)).toBe(0);
  const farther = { ...walk, id: "farther", lat: walk.lat + 0.1 };
  expect(
    filterWalks([farther, walk], DEFAULT_FILTERS, walk).map((w) => w.id),
  ).toEqual([walk.id, "farther"]);
  expect(effort(walk, effortCeilings([walk]))).toBe(1);
});
