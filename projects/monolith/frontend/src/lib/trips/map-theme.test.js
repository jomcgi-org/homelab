import { describe, expect, it, vi } from "vitest";
import { themeBasemap } from "./map-theme.js";

const palette = {
  sheet: "sheet-token",
  paper: "paper-token",
  ink: "ink-token",
  muted: "muted-token",
};

function paint(layers) {
  const map = {
    getStyle: () => ({ layers }),
    setPaintProperty: vi.fn(),
  };
  themeBasemap(map, palette);
  return map.setPaintProperty.mock.calls;
}

describe("themeBasemap", () => {
  it("paints background, fill, line and text symbols from the sheet palette", () => {
    expect(
      paint([
        { id: "ground", type: "background" },
        { id: "water", type: "fill" },
        { id: "park", type: "fill" },
        { id: "road", type: "line" },
        { id: "place", type: "symbol", layout: { "text-field": "{name}" } },
      ]),
    ).toEqual([
      ["ground", "background-color", palette.sheet],
      ["water", "fill-color", palette.paper],
      ["park", "fill-color", palette.paper],
      ["road", "line-color", palette.muted],
      ["place", "text-color", palette.ink],
      ["place", "text-halo-color", palette.sheet],
    ]);
  });

  it.each(["route-0", "route-hit-0", "route", "trip-start", "hillshade"])(
    "leaves the %s overlay paints intact even for a basemap paint type",
    (id) => {
      // Include repaintable types so removing the ID guard fails even when
      // the real overlay is currently a circle or hillshade layer.
      for (const type of ["background", "fill", "line", "symbol"]) {
        expect(
          paint([{ id, type, layout: { "text-field": "{name}" } }]),
        ).toEqual([]);
      }
    },
  );

  it("leaves icon-only symbols, raster and fill-extrusion layers unchanged", () => {
    expect(
      paint([
        { id: "icons", type: "symbol", layout: { "icon-image": "marker" } },
        { id: "no-layout", type: "symbol" },
        { id: "natural_earth", type: "raster" },
        { id: "building-3d", type: "fill-extrusion" },
      ]),
    ).toEqual([]);
  });
});
