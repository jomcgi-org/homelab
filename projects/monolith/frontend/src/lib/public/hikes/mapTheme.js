// The hikes map's colours, resolved from the sheet. Geography uses the shared
// basemap treatment (styles/map-palette.js); the walk markers keep the effort
// ramp, which is data colour, and take their stroke from the sheet so a dot
// stays legible on either scheme.
import {
  applyBasemapPalette,
  readSheetPalette,
} from "$lib/public/styles/map-palette.js";

const ROLES = ["sheet", "ink", "ink-2", "stroke", "band", "water"];

export const WALKS_LAYER = "walks";

export function readMapPalette(host) {
  return readSheetPalette(host, "hikes", ROLES);
}

export function applyMapPalette(map, palette) {
  applyBasemapPalette(map, palette, [WALKS_LAYER]);
  map.setPaintProperty(WALKS_LAYER, "circle-stroke-color", palette.sheet);
}
