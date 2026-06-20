// Test stub for SvelteKit's `$env/dynamic/private` virtual module.
//
// The real module is injected by the SvelteKit build and reflects the server
// process environment. Our vitest config is a bare node environment with no
// SvelteKit plugin (see vitest.config.js), so the virtual specifier does not
// resolve when lib/server/trips-img.js (and the trips server loads that import
// it) are loaded directly. Exposing process.env lets tests set/clear
// IMGPROXY_KEY / IMGPROXY_SALT to exercise the signed and unsigned paths.
export const env = process.env;
