// MapLibre accepts a URL for large GeoJSON. With worker Fetch available, a
// data URL avoids recursively copying every coordinate on the click thread.
// Base64 also avoids its custom-protocol branch, which matches any "://".
export function geoJSONUrl(data) {
  // btoa accepts Latin-1. JSON escapes preserve every UTF-16 code unit,
  // including astral characters, without requiring a newer base64 API.
  const ascii = JSON.stringify(data).replace(
    /[\u0080-\uffff]/g,
    (character) =>
      `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  return `data:application/json;base64,${btoa(ascii)}`;
}
