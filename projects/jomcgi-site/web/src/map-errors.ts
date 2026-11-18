// MapLibre fires "error" for a tile that fails or is cancelled while the
// camera moves (for example the low-zoom tiles it drops when fitBounds jumps
// to Scotland, or tiles dropped during a drag). Such an event carries the
// tile, and the map usually keeps working. An error without a tile (the
// style, a TileJSON source, WebGL) fails the map at once. After tile errors,
// the map has failed only if the settled view renders no base-map feature;
// rendered features include tiles MapLibre reused from earlier views.
interface MapLike {
  on(type: string, listener: (event: any) => void): unknown;
  queryRenderedFeatures(): { source: string }[];
}

export function watchMapHealth(
  map: MapLike,
  overlays: readonly string[],
  onFailure: () => void,
): void {
  let tileErrors = 0;
  map.on("movestart", () => {
    tileErrors = 0;
  });
  map.on("error", (event) => {
    if (!event?.tile) onFailure();
    else tileErrors++;
  });
  map.on("idle", () => {
    if (tileErrors === 0) return;
    const baseMap = map
      .queryRenderedFeatures()
      .some((feature) => !overlays.includes(feature.source));
    if (!baseMap) onFailure();
  });
}
