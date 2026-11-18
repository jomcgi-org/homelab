// MapLibre fires "error" for a single tile that fails or is cancelled while the
// camera moves, for example the low-zoom tiles it drops when fitBounds jumps
// to Scotland. Such an event carries the tile, and the map keeps working. The
// map has failed when an error carries no tile (the style, a TileJSON source,
// WebGL), or when it settles after tile errors without one base-map tile.
interface MapEvents {
  on(type: string, listener: (event: any) => void): unknown;
}

export function watchMapHealth(
  map: MapEvents,
  overlays: readonly string[],
  onFailure: () => void,
): void {
  let loadedTiles = 0;
  let failedTiles = 0;
  map.on("error", (event) => {
    if (!event?.tile) onFailure();
    else failedTiles++;
  });
  map.on("data", (event) => {
    if (
      event?.dataType === "source" &&
      event.tile &&
      !overlays.includes(event.sourceId)
    )
      loadedTiles++;
  });
  map.on("idle", () => {
    if (failedTiles > 0 && loadedTiles === 0) onFailure();
  });
}
