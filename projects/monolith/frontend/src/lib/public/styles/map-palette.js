// A MapLibre basemap drawn in the sheet's own tones.
//
// MapLibre cannot consume CSS variables or color-mix(), so a map reads the
// sheet roles it needs through the browser (a probe element inside the map's
// own scope, so the page's scheme and any page-local overrides apply), then
// repaints the hosted vector style in place. setStyle would discard sources,
// selection and listeners and could refit the camera; paint changes keep all
// of that. Every map on the site (stars, hikes) shares this so geography reads
// the same way on each, and the data layers stay the one coloured thing.

// Resolve `--<prefix>-<role>` for each role to an rgba() string MapLibre
// accepts. The caller's scope defines those custom properties from the sheet
// tokens, so a day/night flip or a system change resolves here on the next
// read.
export function readSheetPalette(host, prefix, roles) {
  const probe = document.createElement("span");
  probe.hidden = true;
  host.append(probe);
  const context = document.createElement("canvas").getContext("2d", {
    willReadFrequently: true,
  });
  try {
    return Object.fromEntries(
      roles.map((role) => {
        probe.style.color = `var(--${prefix}-${role})`;
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = getComputedStyle(probe).color;
        context.fillRect(0, 0, 1, 1);
        const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
        return [role, `rgba(${r}, ${g}, ${b}, ${a / 255})`];
      }),
    );
  } finally {
    probe.remove();
  }
}

// Repaint every basemap layer from a palette of sheet roles (sheet, ink-2,
// stroke, band, water). Layers named in `keep` are the map's own data layers
// and are left for the caller to paint.
export function applyBasemapPalette(map, palette, keep = []) {
  const kept = new Set(keep);
  for (const layer of map.getStyle().layers) {
    if (kept.has(layer.id)) continue;
    const water = /water/i.test(`${layer["source-layer"] ?? ""} ${layer.id}`);
    const paint = (name, value) => map.setPaintProperty(layer.id, name, value);
    if (layer.type === "background") paint("background-color", palette.sheet);
    if (layer.type === "fill") {
      paint("fill-color", water ? palette.water : palette.band);
      if (layer.paint?.["fill-outline-color"])
        paint("fill-outline-color", palette.stroke);
    }
    if (layer.type === "raster") {
      // Low-zoom relief is raster. Keep its texture neutral and within the
      // sheet's luminance range instead of leaving a bright map in dark mode.
      const channels = palette.sheet
        .match(/[\d.]+/g)
        .slice(0, 3)
        .map(Number);
      const light = channels.reduce((sum, value) => sum + value, 0) / (3 * 255);
      paint("raster-saturation", -1);
      paint("raster-brightness-min", Math.max(0, light - 0.12));
      paint("raster-brightness-max", Math.min(1, light + 0.12));
    }
    if (layer.type === "fill-extrusion")
      paint("fill-extrusion-color", palette.band);
    if (layer.type === "line")
      paint("line-color", water ? palette.water : palette.stroke);
    if (layer.type === "symbol" && layer.layout?.["text-field"]) {
      paint("text-color", palette["ink-2"]);
      paint("text-halo-color", palette.sheet);
    }
  }
}
