// The stars map's colours, resolved from the sheet. The basemap treatment is
// shared with every other map on the site (styles/map-palette.js); this
// module adds the stars data roles and paints the two stars layers.
import {
  applyBasemapPalette,
  readSheetPalette,
} from "$lib/public/styles/map-palette.js";

const ROLES = [
  "sheet",
  "ink",
  "ink-2",
  "stroke",
  "band",
  "water",
  "low",
  "quarter",
  "mid",
  "upper",
  "high",
];

export function readMapPalette(host) {
  return readSheetPalette(host, "stars", ROLES);
}

export function cellColors(palette) {
  return [
    "interpolate",
    ["linear"],
    ["get", "score"],
    0,
    palette.low,
    35,
    palette.quarter,
    65,
    palette.mid,
    85,
    palette.upper,
    100,
    palette.high,
  ];
}

export function markerColors(palette) {
  return [
    "step",
    ["get", "score"],
    palette["ink-2"],
    35,
    palette.low,
    65,
    palette.quarter,
  ];
}

// Change paint only. setStyle would discard data sources, selected features and
// listeners, and could refit the camera. Symbol placement and all data stay put.
export function applyMapPalette(map, palette) {
  applyBasemapPalette(map, palette, ["sites", "sites-cells"]);
  map.setPaintProperty("sites-cells", "fill-color", cellColors(palette));
  map.setPaintProperty("sites", "circle-color", markerColors(palette));
  map.setPaintProperty("sites", "circle-stroke-color", palette.ink);
}
