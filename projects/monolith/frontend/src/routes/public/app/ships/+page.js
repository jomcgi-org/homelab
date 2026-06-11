// MapLibre needs window/WebGL, so render the page client-side only. The
// +page.server.js load still runs server-side regardless, so the snapshot
// data stays SSR-sourced (the browser never touches /api/ships/*). Same
// pattern as /notes.
export const ssr = false;
