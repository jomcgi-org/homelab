// MapLibre needs window/WebGL, so render the page client-side only. The
// +page.server.js load still runs server-side regardless, so the sites data
// stays SSR-sourced (the browser never touches /api/stars/*). Same pattern as
// /app/hikes and /app/ships.
export const ssr = false;
