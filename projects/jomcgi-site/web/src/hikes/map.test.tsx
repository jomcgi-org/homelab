import { act, render } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { HikesMap } from "./map";
import { FIXTURE_WALK } from "./fixtures";

const fake = vi.hoisted(() => ({
  handlers: new Map<string, (...args: any[]) => void>(),
  setData: vi.fn(),
  setStyle: vi.fn(),
  remove: vi.fn(),
  fitBounds: vi.fn(),
  easeTo: vi.fn(),
  zoom: 9,
}));
vi.mock("maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url", () => ({
  default: "/assets/worker.js",
}));
vi.mock("maplibre-gl", () => ({
  setWorkerUrl: vi.fn(),
  NavigationControl: class {},
  AttributionControl: class {},
  LngLatBounds: class {
    extend() {}
  },
  Map: class {
    setStyle = fake.setStyle;
    addControl() {}
    getCanvas() {
      return document.createElement("canvas");
    }
    on(name: string, ...args: unknown[]) {
      fake.handlers.set(name, args.at(-1) as (...args: any[]) => void);
    }
    addSource() {}
    addLayer() {}
    getStyle() {
      return { layers: [] };
    }
    setPaintProperty() {}
    getSource() {
      return { setData: fake.setData };
    }
    fitBounds = fake.fitBounds;
    remove = fake.remove;
    resize() {}
    getZoom() {
      return fake.zoom;
    }
    easeTo = fake.easeTo;
  },
}));

beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  fake.zoom = 9;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
  fake.handlers.clear();
});
const mapProps = (walks = [FIXTURE_WALK], selectedId = "") => ({
  walks,
  corpus: walks,
  selectedId,
  onSelect: vi.fn(),
});
it("sends GeoJSON objects, updates the filtered markers and selects the clicked walk", async () => {
  const props = mapProps();
  const view = render(<HikesMap {...props} />);
  expect(fake.setStyle).toHaveBeenCalledExactlyOnceWith(
    "https://tiles.openfreemap.org/styles/liberty",
  );
  await act(async () => fake.handlers.get("load")!());
  expect(fake.setData).toHaveBeenLastCalledWith({
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { id: FIXTURE_WALK.id, effort: 1 },
        geometry: {
          type: "Point",
          coordinates: [FIXTURE_WALK.lon, FIXTURE_WALK.lat],
        },
      },
    ],
  });
  fake.handlers.get("click")!({
    features: [{ properties: { id: FIXTURE_WALK.id } }],
  });
  expect(props.onSelect).toHaveBeenCalledWith(FIXTURE_WALK.id);
  view.rerender(<HikesMap {...props} walks={[]} />);
  expect(fake.setData).toHaveBeenLastCalledWith({
    type: "FeatureCollection",
    features: [],
  });
  view.unmount();
  expect(fake.remove).toHaveBeenCalledOnce();
});

it("recenters only on selection changes, preserving a reader's pan and zoom on clock/data updates", async () => {
  const second = { ...FIXTURE_WALK, id: "second", lon: -5 };
  const props = mapProps([FIXTURE_WALK, second], FIXTURE_WALK.id);
  const view = render(<HikesMap {...props} />);
  await act(async () => fake.handlers.get("load")!());
  expect(fake.easeTo).toHaveBeenCalledExactlyOnceWith({
    center: [FIXTURE_WALK.lon, FIXTURE_WALK.lat],
    zoom: 9,
    duration: 0,
  });
  fake.easeTo.mockClear();
  fake.fitBounds.mockClear();
  fake.zoom = 5; // A reader zoomed out after selecting the walk.
  // The planner's minute tick and a fresh snapshot both create new arrays.
  for (let tick = 0; tick < 3; tick++)
    view.rerender(
      <HikesMap
        {...props}
        walks={props.walks.map((w) => ({ ...w }))}
        corpus={[...props.corpus]}
      />,
    );
  expect(fake.easeTo).not.toHaveBeenCalled();
  expect(fake.fitBounds).not.toHaveBeenCalled();
  view.rerender(<HikesMap {...props} selectedId={second.id} />);
  expect(fake.easeTo).toHaveBeenCalledExactlyOnceWith({
    center: [second.lon, second.lat],
    zoom: 9,
    duration: 0,
  });
  fake.easeTo.mockClear();
  view.rerender(<HikesMap {...props} selectedId="" />);
  expect(fake.easeTo).not.toHaveBeenCalled();
  view.rerender(<HikesMap {...props} selectedId={second.id} />);
  expect(fake.easeTo).toHaveBeenCalledOnce();
});

it("keeps the map for a single failed or cancelled tile until it settles with no base map", async () => {
  const view = render(<HikesMap {...mapProps()} />);
  await act(async () => fake.handlers.get("load")!());
  const status = () => view.queryByText(/The map could not load completely/);
  await act(async () =>
    fake.handlers.get("error")!({
      error: new Error("AJAXError: Failed to fetch (0)"),
      sourceId: "openmaptiles",
      tile: {},
    }),
  );
  expect(status()).toBeNull();
  // Settling with tile errors and no base-map tile is a failed map.
  await act(async () => fake.handlers.get("idle")!());
  expect(status()).not.toBeNull();
  view.unmount();
});

it("reports an error without a tile at once", async () => {
  const view = render(<HikesMap {...mapProps()} />);
  await act(async () => fake.handlers.get("load")!());
  const status = () => view.queryByText(/The map could not load completely/);
  await act(async () =>
    fake.handlers.get("error")!({ error: new Error("style") }),
  );
  expect(status()).not.toBeNull();
  view.unmount();
});
