// MapLibre needs window/WebGL, so render the page client-side only. The
// +page.server.js load still runs server-side regardless, so the walks data
// stays SSR-sourced (the browser never touches /api/hikes/*). Same pattern as
// /app/ships.
export const ssr = false;
