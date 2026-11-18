// MapLibre fires "error" for a single tile that fails or is cancelled while the
// camera moves, for example the low-zoom tiles it drops when fitBounds jumps
// to Scotland. Such an event names the tile or its source, and the map keeps
// working. Only other errors (the style, WebGL) mean the map could not load.
export function isMapFailure(event: object): boolean {
  const named = event as { tile?: unknown; sourceId?: unknown };
  return !named.tile && !named.sourceId;
}
