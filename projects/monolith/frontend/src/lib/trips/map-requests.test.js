// @vitest-environment happy-dom
import { afterEach, expect, it, vi } from "vitest";
import { observeMapRequests } from "./map-requests.js";
import {
  addProtocol,
  removeProtocol,
  getProtocol,
} from "../../../node_modules/maplibre-gl/src/source/protocol_crud.ts";
import {
  AJAXError,
  getArrayBuffer,
} from "../../../node_modules/maplibre-gl/src/util/ajax.ts";
import { VectorTileSource } from "../../../node_modules/maplibre-gl/src/source/vector_tile_source.ts";
import { VectorTileWorkerSource } from "../../../node_modules/maplibre-gl/src/source/vector_tile_worker_source.ts";
import { RasterTileSource } from "../../../node_modules/maplibre-gl/src/source/raster_tile_source.ts";
import { Tile } from "../../../node_modules/maplibre-gl/src/tile/tile.ts";
import { TileManager } from "../../../node_modules/maplibre-gl/src/tile/tile_manager.ts";
import { InViewTiles } from "../../../node_modules/maplibre-gl/src/tile/tile_manager_in_view_tiles.ts";
import { OverscaledTileID } from "../../../node_modules/maplibre-gl/src/tile/tile_id.ts";
import { ImageRequest } from "../../../node_modules/maplibre-gl/src/util/image_request.ts";

const observers = [];
function observer(options = {}) {
  const onFailure = vi.fn();
  const result = observeMapRequests(
    { addProtocol, removeProtocol, AJAXError },
    { onFailure, ...options },
  );
  observers.push(result);
  return { ...result, onFailure };
}
afterEach(() => {
  observers.splice(0).forEach((item) => item.dispose());
  ImageRequest.resetRequestQueue();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("fetches the original URL once and preserves bytes, request options and expiry metadata", async () => {
  const bytes = new Uint8Array([0, 1, 255]);
  const fetch = vi.fn().mockResolvedValue(
    new Response(bytes, {
      headers: {
        "Cache-Control": "max-age=60",
        Expires: "Sun, 15 Nov 2026 01:00:00 GMT",
        ETag: '"tile-v1"',
      },
    }),
  );
  vi.stubGlobal("fetch", fetch);
  const requests = observer();
  const url = "https://tiles.example/1/0/0.pbf?version=a%2Fb&scale=2";
  const controller = new AbortController();
  const options = {
    headers: { Accept: "application/x-protobuf" },
    credentials: "include",
    cache: "reload",
    referrerPolicy: "no-referrer",
    method: "GET",
  };
  const result = await getArrayBuffer(
    { ...requests.transformRequest(url, "Tile"), ...options },
    controller,
  );
  expect(fetch).toHaveBeenCalledExactlyOnceWith(url, {
    ...options,
    body: undefined,
    signal: controller.signal,
  });
  expect(new Uint8Array(result.data)).toEqual(bytes);
  expect(result).toMatchObject({
    cacheControl: "max-age=60",
    expires: "Sun, 15 Nov 2026 01:00:00 GMT",
    etag: '"tile-v1"',
  });
  expect(requests.onFailure).not.toHaveBeenCalled();
});

function sourceMap(requests) {
  return {
    getPixelRatio: () => 1,
    _requestManager: { transformRequest: requests.transformRequest },
    _refreshExpiredTiles: true,
    style: { projection: { subdivisionGranularity: {} } },
    painter: {},
  };
}

function vectorSource(requests) {
  // Execute the installed source AND worker fetch/parse path. No HTTP status
  // or Tile state is synthesized by the test double.
  const worker = new VectorTileWorkerSource(
    { sendAsync: vi.fn() },
    { familiesBySource: { basemap: {} } },
    [],
  );
  const actor = { sendAsync: ({ data }) => worker.loadTile(data) };
  const source = new VectorTileSource(
    "basemap",
    { type: "vector" },
    {
      waitForInitComplete: async () => {},
      getReadyActor: () => actor,
    },
  );
  source.tiles = ["https://tiles.example/{z}/{x}/{y}.pbf"];
  source.map = sourceMap(requests);
  return source;
}

it("observes vector404 before the real source converts it into a loaded empty tile", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response("missing", { status: 404 }));
  vi.stubGlobal("fetch", fetch);
  const requests = observer();
  const source = vectorSource(requests);
  const errors = vi.fn();
  source.on("error", errors);
  const tile = new Tile(new OverscaledTileID(0, 0, 0, 0, 0), 512);
  await source.loadTile(tile);
  expect(tile.state).toBe("loaded");
  expect(tile.latestFeatureIndex).toBeUndefined();
  expect(errors).not.toHaveBeenCalled();
  expect(requests.onFailure).toHaveBeenCalledOnce();
  expect(requests.onFailure.mock.calls[0][0]).toMatchObject({ status: 404 });
  expect(fetch).toHaveBeenCalledOnce();
});

it("accepts a valid empty vector200, then catches a404 on its expiry reload", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(
      new Response(new Uint8Array(), {
        headers: { "Cache-Control": "max-age=60", ETag: '"empty"' },
      }),
    )
    .mockResolvedValueOnce(new Response("missing", { status: 404 }));
  vi.stubGlobal("fetch", fetch);
  const requests = observer();
  const source = vectorSource(requests);
  const tile = new Tile(new OverscaledTileID(0, 0, 0, 0, 0), 512);
  await source.loadTile(tile);
  expect(tile.state).toBe("loaded");
  expect(tile.buckets).toEqual({});
  expect(tile.latestFeatureIndex).toBeTruthy();
  expect(tile.latestRawTileData.byteLength).toBe(0);
  expect(tile.expirationTime).toBeGreaterThan(Date.now());
  expect(tile.etag).toBe('"empty"');
  expect(requests.onFailure).not.toHaveBeenCalled();
  tile.state = "expired";
  await source.loadTile(tile);
  expect(tile.state).toBe("loaded");
  expect(requests.onFailure).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledTimes(2);
});

it("catches a real raster expiry404 without a loading event or another frame", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response("missing", { status: 404 }));
  vi.stubGlobal("fetch", fetch);
  const requests = observer();
  const source = new RasterTileSource("basemap", { type: "raster" });
  source.tiles = ["https://tiles.example/{z}/{x}/{y}.png"];
  source.map = sourceMap(requests);
  const loading = vi.fn();
  const errors = vi.fn();
  source.on("dataloading", loading);
  source.on("error", errors);
  const tile = new Tile(new OverscaledTileID(0, 0, 0, 0, 0), 256);
  tile.state = "loaded";
  const manager = Object.assign(Object.create(TileManager.prototype), {
    _source: source,
    _inViewTiles: new InViewTiles(),
    update: vi.fn(),
  });
  manager._inViewTiles.setTile(tile.tileID.key, tile);
  await manager._reloadTile(tile.tileID.key, "expired");
  expect(loading).not.toHaveBeenCalled();
  expect(errors).not.toHaveBeenCalled();
  expect(tile.state).toBe("errored");
  expect(requests.onFailure).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledOnce();
});

it("passes empty raster responses to the real image path without an error", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response(null, {
        status: 204,
        headers: { "Cache-Control": "max-age=60" },
      }),
    ),
  );
  const requests = observer();
  const response = await ImageRequest.getImage(
    requests.transformRequest("https://tiles.example/empty.png", "Tile"),
    new AbortController(),
  );
  expect(response.data).toBeNull();
  expect(response.cacheControl).toBe("max-age=60");
  expect(requests.onFailure).not.toHaveBeenCalled();
});

it("reports network failures without retrying or duplicating a request", async () => {
  const fetch = vi.fn().mockRejectedValue(new TypeError("offline"));
  vi.stubGlobal("fetch", fetch);
  const requests = observer();
  await expect(
    getArrayBuffer(
      requests.transformRequest("https://tiles.example/a.pbf", "Tile"),
      new AbortController(),
    ),
  ).rejects.toThrow("offline");
  expect(requests.onFailure).toHaveBeenCalledOnce();
  expect(fetch).toHaveBeenCalledOnce();
});

it("aborts active requests and ignores both late responses and queued old generations", async () => {
  let resolve;
  const fetch = vi.fn().mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done;
      }),
  );
  vi.stubGlobal("fetch", fetch);
  const requests = observer();
  const old = requests.transformRequest("https://tiles.example/a.pbf", "Tile");
  const controller = new AbortController();
  const pending = getArrayBuffer(old, controller);
  requests.reset();
  expect(controller.signal.aborted).toBe(true);
  resolve(new Response("late", { status: 500 }));
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  await expect(
    getArrayBuffer(old, new AbortController()),
  ).rejects.toMatchObject({ name: "AbortError" });
  expect(fetch).toHaveBeenCalledOnce();
  expect(requests.onFailure).not.toHaveBeenCalled();
});

it("keeps registrations independent and disposes pending work", async () => {
  const first = observer();
  const second = observer();
  const firstUrl = first.transformRequest(
    "https://tiles.example/a.pbf",
    "Tile",
  ).url;
  const secondUrl = second.transformRequest(
    "https://tiles.example/a.pbf",
    "Tile",
  ).url;
  expect(firstUrl).not.toBe(secondUrl);
  let resolve;
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    ),
  );
  const controller = new AbortController();
  const pending = getArrayBuffer({ url: firstUrl }, controller);
  first.dispose();
  expect(controller.signal.aborted).toBe(true);
  expect(getProtocol(firstUrl)).toBeUndefined();
  expect(getProtocol(secondUrl)).toBeTypeOf("function");
  resolve(new Response(new Uint8Array()));
  await expect(pending).rejects.toMatchObject({ name: "AbortError" });
  expect(first.onFailure).not.toHaveBeenCalled();
});

it("leaves style, glyph and optional terrain requests untouched", () => {
  const prefix = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/";
  const requests = observer({ optionalTilePrefix: prefix });
  for (const [url, type] of [
    ["https://tiles.example/style.json", "Style"],
    ["https://tiles.example/font.pbf", "Glyphs"],
    [`${prefix}0/0/0.png`, "Tile"],
  ])
    expect(requests.transformRequest(url, type)).toEqual({ url });
});
