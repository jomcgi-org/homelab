// Test stub for SvelteKit's `$app/environment` virtual module.
//
// The real module is injected by the SvelteKit build. Our vitest config is a
// bare node environment with no SvelteKit plugin (see vitest.config.js), so the
// virtual specifier does not resolve when a server `load` is imported directly.
// cache-headers.js imports `version` from `$app/environment` to fold the build
// version into page ETags; a fixed value here keeps those ETag assertions
// deterministic across runs.
export const version = "testbuild";
export const browser = false;
export const dev = false;
export const building = false;
