// Same as private/notes: the page is a canvas-driven force graph, so
// SSR adds latency without UX value and avoids Svelte 5 hydration
// issues with d3-mounted components.
export const ssr = false;
