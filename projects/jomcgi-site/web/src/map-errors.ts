// MapLibre fires "error" for a single tile that fails or is cancelled while the
// camera moves, for example the low-zoom tiles it drops when fitBounds jumps
// to Scotland. Such an event carries the tile, and the map keeps working.
// Every other error fails the map, including a source error without a tile
// (a failed TileJSON request leaves the base map with no tiles at all).
export function isMapFailure(event: object): boolean {
  return !(event as { tile?: unknown }).tile;
}
