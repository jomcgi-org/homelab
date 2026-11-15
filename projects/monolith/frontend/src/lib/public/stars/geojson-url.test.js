// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from "vitest";
import { geoJSONUrl } from "./geojson-url.js";
import { GeoJSONSource } from "../../../../node_modules/maplibre-gl/src/source/geojson_source.ts";
import { GeoJSONWorkerSource } from "../../../../node_modules/maplibre-gl/src/source/geojson_worker_source.ts";
import { serialize } from "../../../../node_modules/maplibre-gl/src/util/web_worker_transfer.ts";

const collection = {
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      id: "Àird 🌌 https://example.test/site",
      geometry: { type: "Point", coordinates: [-4.5, 55.083] },
      properties: { id: "Àird 🌌 https://example.test/site", score: 72.345 },
    },
    {
      type: "Feature",
      id: "square",
      geometry: {
        type: "Polygon",
        coordinates: [
          [
            [-4.6, 55],
            [-4.4, 55],
            [-4.4, 55.2],
            [-4.6, 55.2],
            [-4.6, 55],
          ],
        ],
      },
      properties: { id: "square", score: 100 },
    },
  ],
};

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("historical GeoJSON URL transport", () => {
  it("preserves complete JSON values, order and Unicode without a custom protocol marker", async () => {
    const input = structuredClone(collection);
    input.features[0].properties.extra = [
      "\u0000",
      "\ud800",
      "e\u0301",
      '"\\',
      null,
    ];
    const original = structuredClone(input);
    const url = geoJSONUrl(input);
    expect(url).toMatch(/^data:application\/json;base64,/);
    expect(url).not.toContain("://");
    expect(JSON.parse(atob(url.split(",")[1]))).toEqual(original);
    expect(input).toEqual(original);
    expect(await (await fetch(url)).json()).toEqual(original);
  });

  it("preserves an empty collection", async () => {
    const empty = { type: "FeatureCollection", features: [] };
    expect(await (await fetch(geoJSONUrl(empty))).json()).toEqual(empty);
  });

  it("runs the installed source and worker URL path without a main-thread resource bounce", async () => {
    // Reproduce the worker test used by ajax.makeRequest. A blob:https URL
    // would hit this actor; the data URL must use the worker's own Fetch path.
    class WorkerScope {}
    vi.stubGlobal("WorkerGlobalScope", WorkerScope);
    const workerScope = new WorkerScope();
    const resourceBounce = vi.fn(() => {
      throw new Error("GeoJSON was sent back to the main thread for fetching");
    });
    workerScope.worker = {
      referrer: "https://jomcgi.dev/app/stars",
      actor: { sendAsync: resourceBounce },
    };
    vi.stubGlobal("self", workerScope);
    if (!globalThis.ImageData) vi.stubGlobal("ImageData", class ImageData {});
    // happy-dom subclasses its Request implementation, unlike the browser's
    // own signal accessor. Match the Fetch capability gate used by MapLibre.
    class WorkerRequest extends Request {
      get signal() {
        return super.signal;
      }
    }
    vi.stubGlobal("Request", WorkerRequest);
    const requests = vi.spyOn(globalThis, "fetch");
    const worker = new GeoJSONWorkerSource(
      { sendAsync: vi.fn() },
      { familiesBySource: {} },
      [],
    );
    const messages = [];
    const actor = {
      sendAsync: ({ data }) => {
        // Actor's actual serializer receives only the URL and source options.
        const message = structuredClone(serialize(data, []));
        messages.push(structuredClone(message));
        return worker.loadData(message);
      },
    };
    const source = new GeoJSONSource(
      "history",
      { type: "geojson", data: { type: "FeatureCollection", features: [] } },
      { getActor: async () => actor },
    );
    source.map = { _requestManager: { transformRequest: (url) => ({ url }) } };
    const errors = vi.fn();
    source.on("error", errors);
    await source.setData(geoJSONUrl(collection));
    expect(errors).not.toHaveBeenCalled();
    expect(resourceBounce).not.toHaveBeenCalled();
    expect(requests).toHaveBeenCalledOnce();
    expect(requests.mock.calls[0][0].url).toMatch(
      /^data:application\/json;base64,/,
    );
    expect(messages).toHaveLength(1);
    expect(messages[0].data).toBeUndefined();
    expect(await source.getData()).toEqual(collection);
    const tile = worker.loadVectorTile({
      tileID: { canonical: { z: 0, x: 0, y: 0 } },
    });
    expect(
      Object.values(tile.vectorTile.layers).some((layer) => layer.length > 0),
    ).toBe(true);

    // A later update changes the source's full data and keeps the same worker.
    const next = structuredClone(collection);
    next.features.reverse();
    next.features[0].properties.score = 12;
    await source.setData(geoJSONUrl(next));
    expect(await source.getData()).toEqual(next);
    expect(resourceBounce).not.toHaveBeenCalled();
    expect(requests).toHaveBeenCalledTimes(2);

    // Two changes before the first worker load settles must keep the last
    // complete collection. Neither update depends on object-URL lifetime.
    const latest = structuredClone(next);
    latest.features[1].properties.score = 33;
    const pending = source.setData(geoJSONUrl(collection));
    const final = source.setData(geoJSONUrl(latest));
    await Promise.all([pending, final]);
    expect(await source.getData()).toEqual(latest);
    expect(errors).not.toHaveBeenCalled();
    expect(resourceBounce).not.toHaveBeenCalled();
  });
});
